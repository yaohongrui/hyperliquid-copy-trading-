import unittest

from core.engine import Engine
from core.models import Order, Position
from core.symbols import SymbolMap
from exchanges.ccxt_exchange import CcxtExchange
from sources.hyperliquid_ws import HyperliquidWS


class RecordingExchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self, live=0):
        self.live = live
        self.orders = []
        self.cancelled = []
        self.cancel_all_calls = 0

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        return self.live

    async def entry_quote(self, symbol, side):
        return 1

    async def fetch_position_detail(self, symbol):
        return None

    async def create_order(self, symbol, side, amount, order_type="market", price=None, reduce_only=False):
        order_id = f"o{len(self.orders) + 1}"
        self.orders.append((symbol, side, amount, order_type, reduce_only))
        status = "closed" if order_type == "market" else "open"
        if status == "closed":
            self.live += amount if side == "buy" else -amount
        return Order(order_id, symbol, side, amount, price, status, reduce_only)

    async def cancel_order(self, order_id, symbol=None):
        self.cancelled.append((order_id, symbol))

    async def cancel_all_orders(self):
        self.cancel_all_calls += 1
        return 0


class HardStopExchange(RecordingExchange):
    config = {"fixed_balance_usdt": 100, "hard_stop": {"max_daily_loss_usdt": 10}}

    def __init__(self, live=0):
        super().__init__(live)
        self.equity_value = 100
        self.close_calls = 0
        self.fail_closes = 0
        self.has_positions = bool(live)

    async def equity(self):
        return self.equity_value

    async def has_open_positions(self):
        return self.has_positions

    async def close_all_positions(self):
        self.close_calls += 1
        if self.close_calls <= self.fail_closes:
            raise RuntimeError("close failed")
        self.has_positions = False
        return 1


class Notifier:
    def __init__(self):
        self.stops = 0

    async def notify_hard_stop(self, equity, baseline, loss):
        self.stops += 1


class PendingOrderTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, exchange=None):
        cfg = {"total_margin": 100, "coefficient": 1,
               "late_entry_enabled": True, "late_entry_offset_pct": 1}
        return Engine(exchange or RecordingExchange(), {"L": cfg})

    async def test_leader_close_cancels_unfilled_limit_entry(self):
        exchange = RecordingExchange()
        engine = self.engine(exchange)
        await engine.process("L", {})  # first snapshot is startup; no signals
        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertEqual(exchange.orders, [("S", "buy", 100, "limit", False)])
        self.assertIn("L:S", engine.pending_orders)

        # The leader exits before the limit fills: cancel instead of leaving
        # an order that would later create an orphan position.
        await engine.process("L", {})
        self.assertEqual(exchange.cancelled, [("o1", "S")])
        self.assertNotIn("L:S", engine.pending_orders)

    async def test_pending_order_expires_after_ttl(self):
        exchange = RecordingExchange()
        engine = self.engine(exchange)
        await engine.process("L", {})
        await engine.process("L", {"S": Position("S", 100, 1)})
        created = engine.pending_orders["L:S"]["created_at"]

        await engine.expire_pending_orders(now=created + 10)
        self.assertIn("L:S", engine.pending_orders)
        await engine.expire_pending_orders(now=created + 61)
        self.assertNotIn("L:S", engine.pending_orders)
        self.assertEqual(exchange.cancelled, [("o1", "S")])

    async def test_reconcile_keeps_owner_while_limit_is_working(self):
        exchange = RecordingExchange()
        engine = self.engine(exchange)
        await engine.process("L", {})
        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertIn("__default__:S", engine.owners)

        await engine.reconcile_protections()
        self.assertIn("__default__:S", engine.owners)
        self.assertIn("L:S", engine.pending_orders)
        self.assertEqual(exchange.cancelled, [])


class HardStopTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_flatten_retries_and_notifies_once(self):
        exchange = HardStopExchange()
        notifier = Notifier()
        engine = Engine(exchange, {"L": {"total_margin": 100}}, notifier=notifier)
        self.assertFalse(await engine.check_hard_stop())  # establishes baseline

        exchange.equity_value = 80
        exchange.fail_closes = 1
        self.assertTrue(await engine.check_hard_stop())
        self.assertTrue(engine.risk.locked)
        self.assertFalse(engine.risk.flattened)
        self.assertEqual(exchange.close_calls, 1)
        self.assertEqual(notifier.stops, 1)

        self.assertTrue(await engine.check_hard_stop())
        self.assertTrue(engine.risk.flattened)
        self.assertEqual(exchange.close_calls, 2)
        self.assertEqual(notifier.stops, 1)

        self.assertTrue(await engine.check_hard_stop())
        self.assertEqual(exchange.close_calls, 2)

    async def test_locked_state_blocks_new_exposure_without_paused(self):
        exchange = RecordingExchange()
        engine = Engine(exchange, {"L": {"total_margin": 100}})
        engine.risk.locked = True  # persisted lock after a restart
        engine.paused = False

        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertEqual(exchange.orders, [])

    async def test_hard_stop_cancels_pending_limits_before_flatten(self):
        exchange = HardStopExchange()
        engine = Engine(exchange, {"L": {
            "total_margin": 100, "coefficient": 1,
            "late_entry_enabled": True, "late_entry_offset_pct": 1,
        }})
        await engine.process("L", {})
        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertIn("L:S", engine.pending_orders)

        self.assertFalse(await engine.check_hard_stop())  # baseline
        exchange.equity_value = 80
        await engine.check_hard_stop()
        self.assertIn(("o1", "S"), exchange.cancelled)
        self.assertNotIn("L:S", engine.pending_orders)
        self.assertEqual(exchange.cancel_all_calls, 1)


class CloseAllPositionsTests(unittest.IsolatedAsyncioTestCase):
    class Client:
        def __init__(self):
            self.positions = [{
                "symbol": "BTC/USDT:USDT", "contracts": 2,
                "contractSize": 0.5, "side": "long",
            }]
            self.markets = {"BTC/USDT:USDT": {"contractSize": 0.5}}

        async def fetch_positions(self):
            return list(self.positions)

    async def test_contracts_are_converted_to_base_quantity(self):
        instance = CcxtExchange.__new__(CcxtExchange)
        instance.client = self.Client()
        instance.name = "gate"
        instance.config = {}
        instance.symbol_map = SymbolMap()
        recorded = []

        async def create_order(symbol, side, amount, order_type="market", price=None, reduce_only=False):
            recorded.append((symbol, side, amount, order_type, reduce_only))
            instance.client.positions = []
            return Order("x", symbol, side, amount, price, "closed", reduce_only)

        async def cancel_all_orders():
            return 0

        instance.create_order = create_order
        instance.cancel_all_orders = cancel_all_orders

        closed = await instance.close_all_positions()
        self.assertEqual(closed, 1)
        # 2 contracts * 0.5 base per contract = 1.0 BTC, not 2.0.
        self.assertEqual(recorded, [("BTC/USDT:USDT", "sell", 1.0, "market", True)])

    async def test_remaining_position_raises_so_the_caller_retries(self):
        instance = CcxtExchange.__new__(CcxtExchange)
        instance.client = self.Client()
        instance.name = "gate"
        instance.config = {}
        instance.symbol_map = SymbolMap()

        async def create_order(*args, **kwargs):
            raise RuntimeError("rejected")

        async def cancel_all_orders():
            return 0

        instance.create_order = create_order
        instance.cancel_all_orders = cancel_all_orders

        with self.assertRaisesRegex(RuntimeError, "failed to flatten"):
            await instance.close_all_positions(attempts=1)


class AddressChangeTests(unittest.IsolatedAsyncioTestCase):
    async def test_address_change_resets_baseline_and_pending_orders(self):
        exchange = RecordingExchange()
        config = {"total_margin": 100, "coefficient": 1,
                  "late_entry_enabled": True, "late_entry_offset_pct": 1,
                  "address": "0x" + "a" * 40}
        engine = Engine(exchange, {"L": config})
        await engine.process("L", {})
        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertIn("L:S", engine.pending_orders)
        engine.stopped["L:S"] = True
        engine.entered["L:S"] = True
        engine.small_order_carry["L:S"] = {"side": "buy", "amount": 5}

        changed = await engine.update_leader_config("L", {"address": "0x" + "b" * 40})
        self.assertTrue(changed)
        self.assertNotIn("L", engine.previous)
        self.assertNotIn("L:S", engine.stopped)
        self.assertNotIn("L:S", engine.entered)
        self.assertNotIn("L:S", engine.small_order_carry)
        self.assertNotIn("L:S", engine.pending_orders)
        self.assertEqual(exchange.cancelled, [("o1", "S")])

        # The first event from the new address is a startup diff: it opens the
        # new position without emitting false closes for the old address.
        await engine.process("L", {"T": Position("T", 50, 1)})
        self.assertEqual(exchange.orders[-1], ("T", "buy", 50, "market", False))

    async def test_same_address_keeps_baseline(self):
        exchange = RecordingExchange()
        address = "0x" + "a" * 40
        engine = Engine(exchange, {"L": {"total_margin": 100, "address": address}})
        await engine.process("L", {"S": Position("S", 100, 1)})

        changed = await engine.update_leader_config("L", {"address": address})
        self.assertFalse(changed)
        self.assertIn("L", engine.previous)


class FakeWebSocket:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class WebSocketAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_addresses_only_reconnects_when_list_changes(self):
        stream = HyperliquidWS(addresses=["0xa"])
        socket = FakeWebSocket()
        stream._ws = socket

        await stream.set_addresses(["0xa"])
        self.assertFalse(socket.closed)

        await stream.set_addresses(["0xb"])
        self.assertTrue(socket.closed)
        self.assertEqual(stream.addresses, ["0xb"])


class FlattenedStateTests(unittest.TestCase):
    def test_flattened_state_round_trips_and_unlock_clears_it(self):
        from risk.manager import RiskManager
        risk = RiskManager({"max_daily_loss_usdt": 10})
        risk.evaluate(100)
        risk.evaluate(80)
        risk.flattened = True
        restored = RiskManager(risk.config, risk.state())
        self.assertTrue(restored.flattened)
        restored.unlock(80)
        self.assertFalse(restored.locked)
        self.assertFalse(restored.flattened)


if __name__ == "__main__":
    unittest.main()
