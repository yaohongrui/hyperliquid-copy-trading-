import tempfile
import unittest
from pathlib import Path

from core.engine import Engine
from state.store import StateStore


class AutoBreakevenExchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self, entry=100, mark=110, side="long", contracts=2,
                 contract_size=1, fail_protection=False, detail=True):
        self.entry = entry
        self.mark = mark
        self.side = side
        self.contracts = contracts
        self.contract_size = contract_size
        self.fail_protection = fail_protection
        self.detail_available = detail
        self.set_calls = []
        self.cancel_calls = []
        self.cancel_orders_calls = []

    async def fetch_position_detail(self, symbol):
        if not self.detail_available:
            return None
        return {"contracts": self.contracts, "contractSize": self.contract_size,
                "entryPrice": self.entry, "side": self.side, "markPrice": self.mark,
                "unrealizedPnl": 9999}

    async def set_protection(self, symbol, side, amount, trigger):
        self.set_calls.append((symbol, side, amount, trigger))
        if self.fail_protection:
            raise RuntimeError("exchange rejected the stop")
        return "p1"

    async def cancel_protection(self, order_id, symbol):
        self.cancel_calls.append((order_id, symbol))

    async def cancel_protection_orders(self, symbol, keep_id=None):
        self.cancel_orders_calls.append((symbol, keep_id))


class Notifier:
    def __init__(self):
        self.breakevens = []

    async def notify_breakeven(self, leader, symbol, profit, trigger):
        self.breakevens.append((leader, symbol, profit, trigger))


class AutoBreakevenTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, exchange, auto=None, notifier=None, state_store=None, stop=None):
        cfg = {"total_margin": 100}
        if stop is not None:
            cfg["position_stop_loss"] = stop
        engine = Engine(exchange, {"L": cfg}, notifier=notifier,
                        state_store=state_store, auto_breakeven=auto)
        engine.owners = {"__default__:BTC": "L"}
        return engine

    async def test_arms_when_profit_reaches_threshold(self):
        exchange = AutoBreakevenExchange(entry=100, mark=106, contracts=2)
        notifier = Notifier()
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10},
                             notifier=notifier)

        await engine.reconcile_protections()

        self.assertTrue(engine.breakeven["__default__:BTC"])
        self.assertEqual(exchange.set_calls, [("BTC", "sell", 2.0, 100.0)])
        self.assertEqual(notifier.breakevens, [("L", "BTC", 12.0, 100.0)])

    async def test_does_not_arm_below_threshold(self):
        exchange = AutoBreakevenExchange(entry=100, mark=104, contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})

        await engine.reconcile_protections()

        self.assertNotIn("__default__:BTC", engine.breakeven)
        self.assertEqual(exchange.set_calls, [])

    async def test_short_position_uses_a_buy_stop(self):
        exchange = AutoBreakevenExchange(entry=100, mark=90, side="short", contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})

        await engine.reconcile_protections()

        self.assertEqual(exchange.set_calls, [("BTC", "buy", 2.0, 100.0)])

    async def test_leader_override_can_disable_the_global_default(self):
        exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})
        engine.leaders["L"]["auto_breakeven"] = {"enabled": False}

        await engine.reconcile_protections()

        self.assertNotIn("__default__:BTC", engine.breakeven)
        self.assertEqual(exchange.set_calls, [])

    async def test_leader_override_can_set_its_own_threshold(self):
        exchange = AutoBreakevenExchange(entry=100, mark=104, contracts=2)  # profit 8
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})
        engine.leaders["L"]["auto_breakeven"] = {"trigger_usdt": 5}

        await engine.reconcile_protections()

        self.assertTrue(engine.breakeven["__default__:BTC"])

    async def test_already_armed_position_is_not_replaced(self):
        exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})

        await engine.reconcile_protections()
        await engine.reconcile_protections()

        self.assertEqual(exchange.set_calls, [("BTC", "sell", 2.0, 100.0)])

    async def test_missing_detail_is_ignored(self):
        exchange = AutoBreakevenExchange(detail=False)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})

        self.assertIsNone(await engine._maybe_auto_breakeven("L", "BTC", None))

    async def test_unrealized_pnl_field_does_not_override_mark_based_profit(self):
        # The fake reports +9999 unrealized PnL but the mark only implies +4.
        exchange = AutoBreakevenExchange(entry=100, mark=102, contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})

        await engine.reconcile_protections()

        self.assertNotIn("__default__:BTC", engine.breakeven)

    async def test_failed_placement_rolls_back_and_backs_off(self):
        exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2,
                                         fail_protection=True)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})

        await engine.reconcile_protections()

        self.assertNotIn("__default__:BTC", engine.breakeven)
        self.assertEqual(len(exchange.set_calls), 1)
        self.assertGreater(engine._auto_be_retry_at["__default__:BTC"], 0)

        await engine.reconcile_protections()
        self.assertEqual(len(exchange.set_calls), 1)  # retry window still active

    async def test_manual_clear_suppresses_automatic_rearm(self):
        exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})
        await engine.reconcile_protections()

        await engine.clear_breakeven("L", "BTC")
        await engine.reconcile_protections()

        self.assertNotIn("__default__:BTC", engine.breakeven)
        self.assertTrue(engine.auto_breakeven_skip["__default__:BTC"])
        self.assertEqual(len(exchange.set_calls), 1)

    async def test_manual_set_clears_the_cancel_marker(self):
        exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2)
        engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10})
        engine.auto_breakeven_skip["__default__:BTC"] = True

        await engine.set_breakeven("L", "BTC")

        self.assertNotIn("__default__:BTC", engine.auto_breakeven_skip)
        self.assertTrue(engine.breakeven["__default__:BTC"])

    async def test_state_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2)
            engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10},
                                 state_store=store)
            await engine.reconcile_protections()
            await engine.clear_breakeven("L", "BTC")

            restarted = Engine(exchange, {"L": {"total_margin": 100}},
                               state_store=store,
                               auto_breakeven={"enabled": True, "trigger_usdt": 10})

            self.assertNotIn("__default__:BTC", restarted.breakeven)
            self.assertTrue(restarted.auto_breakeven_skip["__default__:BTC"])

    async def test_breakeven_flag_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            exchange = AutoBreakevenExchange(entry=100, mark=110, contracts=2)
            engine = self.engine(exchange, auto={"enabled": True, "trigger_usdt": 10},
                                 state_store=store)
            await engine.reconcile_protections()

            restarted = Engine(exchange, {"L": {"total_margin": 100}}, state_store=store)

            self.assertTrue(restarted.breakeven["__default__:BTC"])


if __name__ == "__main__":
    unittest.main()
