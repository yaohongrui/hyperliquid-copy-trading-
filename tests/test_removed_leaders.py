import tempfile
import unittest
from pathlib import Path

from core.engine import Engine
from state.store import StateStore


class Exchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self):
        self.probed = []

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        self.probed.append(symbol)
        return 0.0

    async def entry_quote(self, symbol, side):
        return 1

    async def fetch_position_detail(self, symbol):
        return None


class RemovedLeaderStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(Path(self._tmp.name) / "state.json")

    async def test_state_of_removed_leader_is_dropped_on_startup(self):
        self.store.save({
            "symbol_owners": {"hl_primary:KORU": "B"},
            "protection": {
                "hl_primary:KORU": {"id": "1", "contracts": 1, "trigger": 1},
            },
            "entered": {"B:KORU": True, "L:S": True},
            "stopped": {"B:KORU": True},
            "small_order_carry": {
                "B:KORU": {"side": "buy", "amount": 1},
            },
            "breakeven": {"hl_primary:KORU": {"price": 1}},
        })
        exchange = Exchange()

        engine = Engine(
            exchange,
            {"L": {"execution_account": "legacy_primary"}},
            state_store=self.store,
        )

        self.assertEqual(engine.owners, {})
        self.assertNotIn("hl_primary:KORU", engine.runtime_state["protection"])
        self.assertNotIn("B:KORU", engine.entered)
        self.assertNotIn("B:KORU", engine.stopped)
        self.assertNotIn("B:KORU", engine.small_order_carry)
        self.assertNotIn("hl_primary:KORU", engine.breakeven)
        # State for a leader that is still configured is untouched.
        self.assertIn("L:S", engine.entered)

        # Reconciliation must not look up the removed leader's account.
        await engine.reconcile_protections()
        self.assertEqual(exchange.probed, [])

        saved = self.store.load()
        self.assertEqual(saved["symbol_owners"], {})
        self.assertNotIn("hl_primary:KORU", saved["protection"])


if __name__ == "__main__":
    unittest.main()
