import time
import unittest

from core.engine import Engine
from core.mirror import OrderMirror
from core.models import Order, Position
from sources.leader_orders import (
    LeaderFillBook,
    LeaderRestingOrder,
    fetch_leader_orders,
)


def resting(oid, side, price, size, mark, symbol="AAA", reduce_only=False):
    return LeaderRestingOrder(
        leader="L", dex="", symbol=symbol, oid=oid, side=side, price=price,
        size=size, mark=mark, reduce_only=reduce_only, timestamp=0.0,
    )


class MirrorExchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self):
        self.positions = {}
        self.orders = []
        self.cancelled = []
        self.order_states = {}
        self.protection = []
        self._ids = 0

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        return self.positions.get(symbol, 0.0)

    async def entry_quote(self, symbol, side):
        return 100

    async def fetch_position_detail(self, symbol):
        return None

    async def create_order(self, symbol, side, amount, order_type="market",
                           price=None, reduce_only=False, post_only=False):
        self._ids += 1
        order_id = f"m{self._ids}"
        self.orders.append((symbol, side, amount, order_type, price, reduce_only, post_only))
        if order_type == "market":
            self.positions[symbol] = self.positions.get(symbol, 0.0) + (
                amount if side == "buy" else -amount)
            return Order(order_id, symbol, side, amount, price, "closed", reduce_only)
        self.order_states[order_id] = {
            "status": "open", "filled": 0.0, "remaining": amount,
            "average": None, "price": price,
        }
        return Order(order_id, symbol, side, amount, price, "open", reduce_only)

    async def cancel_order(self, order_id, symbol=None):
        self.cancelled.append(order_id)
        self.order_states.pop(order_id, None)

    async def fetch_order(self, order_id, symbol=None):
        return self.order_states.get(order_id)

    async def set_protection(self, *args, **kwargs):
        self.protection.append(args)
        return "stop-1"

    async def cancel_protection(self, *args, **kwargs):
        return None

    async def cancel_protection_orders(self, *args, **kwargs):
        return None


def make_engine(exchange, mirror_cfg=None, leader_extra=None):
    cfg = {"total_margin": 100, "coefficient": 1, "enabled": True,
           "mirror_orders": True, "symbol_blacklist": [], "address": "0xleader"}
    cfg.update(leader_extra or {})
    engine = Engine(exchange, {"L": cfg})
    mirror = OrderMirror(engine, mirror_cfg or {
        "enabled": True, "max_orders": 2, "post_only": True,
        "refresh_seconds": 1, "leader_orders_seconds": 10,
    })
    engine.mirror = mirror
    # Tests drive sync() directly; production keeps the 1s anti-churn throttle.
    mirror.min_sync_seconds = 0.0
    return engine, mirror


class SelectionTests(unittest.TestCase):
    def test_nearest_above_and_below(self):
        engine, mirror = make_engine(MirrorExchange())
        candidates = [
            resting("1", "buy", 95, 10, 100),
            resting("2", "buy", 90, 10, 100),
            resting("3", "sell", 105, 10, 100),
            resting("4", "sell", 120, 10, 100),
        ]
        picked = mirror._select(candidates)
        self.assertEqual({order.oid for order in picked}, {"1", "3"})

    def test_one_side_places_one(self):
        engine, mirror = make_engine(MirrorExchange())
        picked = mirror._select([resting("1", "buy", 95, 10, 100),
                                 resting("2", "buy", 90, 10, 100)])
        self.assertEqual([order.oid for order in picked], ["1"])

    def test_max_orders_one_takes_global_nearest(self):
        engine, mirror = make_engine(MirrorExchange(), {"enabled": True, "max_orders": 1})
        picked = mirror._select([resting("1", "buy", 99, 1, 100),
                                 resting("2", "sell", 101, 1, 100)])
        self.assertEqual(len(picked), 1)
        self.assertEqual(picked[0].oid, "1")


class MirrorExecutionTests(unittest.IsolatedAsyncioTestCase):
    def build(self, orders=None, mirror_cfg=None, leader_extra=None):
        exchange = MirrorExchange()
        engine, mirror = make_engine(exchange, mirror_cfg, leader_extra)
        mirror._books[("L", "")] = list(orders or [])
        mirror._book_at["L"] = time.monotonic() + 3600
        return exchange, engine, mirror

    async def test_places_post_only_at_same_price_with_scaled_size(self):
        exchange, engine, mirror = self.build([
            resting("1", "buy", 95, 10, 100),
            resting("2", "sell", 105, 10, 100),
        ])
        await mirror.sync()
        self.assertEqual(exchange.orders, [
            ("AAA", "buy", 10.0, "limit", 95.0, False, True),
            ("AAA", "sell", 10.0, "limit", 105.0, False, True),
        ])

    async def test_cancels_when_leader_cancels(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        self.assertEqual(len(mirror.orders), 1)
        self.assertEqual(engine.owners.get("__default__:AAA"), "L")
        mirror._books[("L", "")] = []
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])
        self.assertEqual(mirror.orders, {})
        self.assertNotIn("__default__:AAA", engine.owners)

    async def test_unfilled_mirror_stays_when_leader_fills(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        fill_time = int((time.time() + 5) * 1000)
        mirror.fills.add("L", [{
            "coin": "AAA", "side": "B", "px": "95", "sz": "10",
            "time": fill_time, "crossed": False, "startPosition": "0",
            "oid": "1", "tid": "t1",
        }])
        mirror._books[("L", "")] = []
        await mirror.sync()
        self.assertEqual(exchange.cancelled, [])
        self.assertTrue(next(iter(mirror.orders.values()))["orphan"])

    async def test_partial_fill_then_leader_cancel_cancels_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        fill_time = int((time.time() + 5) * 1000)
        mirror.fills.add("L", [{
            "coin": "AAA", "side": "B", "px": "95", "sz": "4",
            "time": fill_time, "crossed": False, "startPosition": "0",
            "oid": "1", "tid": "t1",
        }])
        mirror._books[("L", "")] = []
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])
        self.assertEqual(mirror.orders, {})

    async def test_fill_updates_the_cached_leader_book_immediately(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        mirror._fill_floor["L"] = time.time() - 60
        mirror.on_fills("L", [{
            "coin": "AAA", "side": "B", "px": "95", "sz": "4",
            "time": int((time.time() + 1) * 1000), "crossed": False,
            "startPosition": "0", "oid": "1", "tid": "t1",
        }])
        if mirror._sync_task is not None:
            mirror._sync_task.cancel()
        self.assertEqual(mirror._books[("L", "")][0].size, 6.0)

    async def test_order_update_open_adds_to_the_book(self):
        exchange, engine, mirror = self.build([])
        engine.previous["L"] = {"AAA": Position("AAA", 1, mark_price=100)}
        mirror.on_order_updates("L", [{
            "status": "open",
            "order": {"oid": 7, "coin": "AAA", "side": "B", "limitPx": "95",
                      "sz": "10", "orderType": "Limit", "isTrigger": False,
                      "reduceOnly": False, "timestamp": 1000},
        }])
        if mirror._sync_task is not None:
            mirror._sync_task.cancel()
        order = mirror._book("L").get("7")
        self.assertIsNotNone(order)
        self.assertEqual(order.mark, 100)
        self.assertEqual(order.side, "buy")

    async def test_order_update_cancel_cancels_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        mirror.on_order_updates("L", [{
            "status": "canceled",
            "order": {"oid": 1, "coin": "AAA", "side": "B", "limitPx": "95",
                      "sz": "10", "orderType": "Limit", "isTrigger": False,
                      "reduceOnly": False, "timestamp": 1000},
        }])
        if mirror._sync_task is not None:
            mirror._sync_task.cancel()
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])

    async def test_order_update_filled_keeps_mirror_resting(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        mirror.on_order_updates("L", [{
            "status": "filled",
            "order": {"oid": 1, "coin": "AAA", "side": "B", "limitPx": "95",
                      "sz": "10", "orderType": "Limit", "isTrigger": False,
                      "reduceOnly": False, "timestamp": 1000},
        }])
        if mirror._sync_task is not None:
            mirror._sync_task.cancel()
        await mirror.sync()
        self.assertEqual(exchange.cancelled, [])
        self.assertTrue(next(iter(mirror.orders.values()))["orphan"])

    async def test_crossed_fill_on_mirrored_order_cancels_instead_of_orphaning(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        mirror.fills.add("L", [{
            "coin": "AAA", "side": "B", "px": "95", "sz": "10",
            "time": int((time.time() + 5) * 1000), "crossed": True,
            "startPosition": "0", "oid": "1", "tid": "t1",
        }])
        mirror._books[("L", "")] = []
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])
        self.assertEqual(mirror.orders, {})

    async def test_reduce_only_is_clamped_to_position(self):
        exchange, engine, mirror = self.build(
            [resting("1", "sell", 105, 50, 100, reduce_only=True)])
        exchange.positions["AAA"] = 5
        await mirror.sync()
        self.assertEqual(exchange.orders,
                         [("AAA", "sell", 5.0, "limit", 105.0, True, True)])

    async def test_reduce_only_without_matching_position_is_skipped(self):
        exchange, engine, mirror = self.build(
            [resting("1", "sell", 105, 50, 100, reduce_only=True)])
        exchange.positions["AAA"] = -5
        await mirror.sync()
        self.assertEqual(exchange.orders, [])

    async def test_paused_cancels_non_reduce_mirrors(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await mirror.sync()
        engine.paused = True
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])

    async def test_shadow_places_nothing(self):
        exchange, engine, mirror = self.build(
            [resting("1", "buy", 95, 10, 100)],
            {"enabled": True, "max_orders": 2, "shadow": True})
        await mirror.sync()
        self.assertEqual(exchange.orders, [])
        self.assertEqual(mirror.orders, {})

    async def test_below_minimum_quantity_is_skipped(self):
        class SmallExchange(MirrorExchange):
            async def create_order(self, *args, **kwargs):
                from exchanges.base import OrderQuantityTooSmall
                raise OrderQuantityTooSmall("minimum")

        exchange = SmallExchange()
        engine, mirror = make_engine(exchange)
        mirror._books[("L", "")] = [resting("1", "buy", 95, 10, 100)]
        mirror._book_at["L"] = time.monotonic() + 3600
        await mirror.sync()
        self.assertEqual(mirror.orders, {})


class OrphanTests(unittest.IsolatedAsyncioTestCase):
    def build(self, orders=None, mirror_cfg=None):
        exchange = MirrorExchange()
        engine, mirror = make_engine(exchange, mirror_cfg)
        mirror._books[("L", "")] = list(orders or [])
        mirror._book_at["L"] = time.monotonic() + 3600
        return exchange, engine, mirror

    def fill(self, mirror, oid, side="buy", size=10, start=0):
        mirror.fills.add("L", [{
            "coin": "AAA", "side": "B" if side == "buy" else "A",
            "px": "95", "sz": str(size),
            "time": int((time.time() + 5) * 1000),
            "crossed": False, "startPosition": str(start),
            "oid": oid, "tid": f"t-{oid}",
        }])

    async def make_orphan(self, exchange, mirror, oid="1", side="buy", size=10):
        await mirror.sync()
        self.fill(mirror, oid, side, size)
        mirror._books[("L", "")] = []
        await mirror.sync()
        self.assertTrue(next(iter(mirror.orders.values()))["orphan"])

    async def test_paused_cancels_orphan_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await self.make_orphan(exchange, mirror)
        engine.paused = True
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])
        self.assertEqual(mirror.orders, {})

    async def test_paused_keeps_reduce_only_orphan(self):
        exchange, engine, mirror = self.build(
            [resting("1", "sell", 105, 5, 100, reduce_only=True)])
        exchange.positions["AAA"] = 5
        await self.make_orphan(exchange, mirror, side="sell", size=5)
        engine.paused = True
        await mirror.sync()
        self.assertEqual(exchange.cancelled, [])

    async def test_leader_flat_cancels_orphan_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await self.make_orphan(exchange, mirror)
        await engine.process("L", {})  # leader is flat -> orphan is pointless
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])

    async def test_leader_reversal_cancels_orphan_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await self.make_orphan(exchange, mirror)
        await engine.process("L", {"AAA": Position("AAA", -5, mark_price=100)})
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])

    async def test_orphan_ttl_expires_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await self.make_orphan(exchange, mirror)
        item = next(iter(mirror.orders.values()))
        item["orphaned_at"] = time.time() - mirror.orphan_ttl - 1
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])

    async def test_orphans_do_not_consume_slots(self):
        exchange, engine, mirror = self.build([
            resting("1", "buy", 95, 10, 100),
            resting("2", "sell", 105, 10, 100),
        ])
        await mirror.sync()
        self.assertEqual(len(exchange.orders), 2)
        self.fill(mirror, "1", "buy", 10)
        self.fill(mirror, "2", "sell", 10)
        mirror._books[("L", "")] = []
        await mirror.sync()
        self.assertEqual(len(mirror.orders), 2)
        self.assertTrue(all(item.get("orphan") for item in mirror.orders.values()))
        mirror._books[("L", "")] = [resting("9", "buy", 94, 10, 100)]
        await mirror.sync()
        self.assertEqual(len(exchange.orders), 3)
        self.assertEqual(exchange.orders[-1][:2], ("AAA", "buy"))

    async def test_max_orphans_evicts_the_oldest(self):
        exchange, engine, mirror = self.build([
            resting("1", "buy", 95, 10, 100),
            resting("2", "sell", 105, 10, 100),
        ])
        await mirror.sync()
        self.fill(mirror, "1", "buy", 10)
        self.fill(mirror, "2", "sell", 10)
        mirror._books[("L", "")] = []
        await mirror.sync()
        items = {item["leader_oid"]: item for item in mirror.orders.values()}
        items["1"]["orphaned_at"] = time.time() - 10
        items["2"]["orphaned_at"] = time.time()
        mirror.max_orphans = 1
        await mirror.sync()
        self.assertEqual(exchange.cancelled, ["m1"])

    async def test_reduce_signal_cancels_orphan_mirror(self):
        exchange, engine, mirror = self.build([resting("1", "buy", 95, 10, 100)])
        await self.make_orphan(exchange, mirror)
        engine.previous["L"] = {"AAA": Position("AAA", 5, mark_price=100)}
        await engine.process("L", {})
        self.assertIn("m1", exchange.cancelled)


class CreditTests(unittest.IsolatedAsyncioTestCase):
    def build(self):
        exchange = MirrorExchange()
        engine, mirror = make_engine(exchange)
        return exchange, engine, mirror

    def feed(self, mirror, side, size, start, crossed=False, oid="1",
             mirrored=True):
        # Go through the production path (on_fills) instead of re-implementing
        # the credit rules: an entry is only credited when it was mirrored.
        if mirrored:
            mirror._remember_mirrored("L", oid)
        raw = {
            "coin": "AAA", "side": "B" if side == "buy" else "A",
            "px": "95", "sz": str(size),
            "time": int((time.time() + 1) * 1000),
            "crossed": crossed, "startPosition": str(start),
            "oid": oid, "tid": f"t-{oid}",
        }
        mirror.on_fills("L", [raw])
        if mirror._sync_task is not None:
            mirror._sync_task.cancel()

    async def test_maker_entry_is_not_reordered_by_snapshot_engine(self):
        exchange, engine, mirror = self.build()
        await engine.process("L", {})  # startup baseline
        self.feed(mirror, "buy", 5, start=0)
        exchange.positions["AAA"] = 5
        await engine.process("L", {"AAA": Position("AAA", 5, mark_price=100)})
        self.assertEqual(exchange.orders, [])
        self.assertTrue(engine.entered.get("L:AAA"))
        self.assertEqual(engine.owners.get("__default__:AAA"), "L")

    async def test_unmirrored_maker_entry_uses_market_path(self):
        exchange, engine, mirror = self.build()
        await engine.process("L", {})  # startup baseline
        self.feed(mirror, "buy", 5, start=0, oid="unmirrored", mirrored=False)
        await engine.process("L", {"AAA": Position("AAA", 5, mark_price=100)})
        self.assertEqual(exchange.orders,
                         [("AAA", "buy", 5.0, "market", None, False, False)])

    async def test_mirrored_maker_entry_keeps_resting_mirror(self):
        exchange, engine, mirror = self.build()
        mirror._books[("L", "")] = [resting("1", "buy", 95, 5, 100)]
        mirror._book_at["L"] = time.monotonic() + 3600
        await mirror.sync()
        self.assertEqual(len(mirror.orders), 1)
        await engine.process("L", {})  # startup baseline
        self.feed(mirror, "buy", 5, start=0)
        await engine.process("L", {"AAA": Position("AAA", 5, mark_price=100)})
        # Only the resting post-only mirror remains; no market copy.
        self.assertEqual(exchange.orders,
                         [("AAA", "buy", 5.0, "limit", 95.0, False, True)])
        self.assertIn("L:1", mirror.orders)

    async def test_taker_fill_still_uses_market_path(self):
        exchange, engine, mirror = self.build()
        await engine.process("L", {})
        self.feed(mirror, "buy", 5, start=0, crossed=True)
        await engine.process("L", {"AAA": Position("AAA", 5, mark_price=100)})
        self.assertEqual(exchange.orders,
                         [("AAA", "buy", 5.0, "market", None, False, False)])

    async def test_maker_exit_without_mirror_uses_market_path(self):
        exchange, engine, mirror = self.build()
        await engine.process("L", {})  # startup baseline
        await engine.process("L", {"AAA": Position("AAA", 5, mark_price=100)})
        self.assertEqual(exchange.positions.get("AAA"), 5)
        exchange.orders.clear()
        self.feed(mirror, "sell", 5, start=5, oid="exit-1", mirrored=False)
        await engine.process("L", {})
        self.assertEqual(exchange.orders,
                         [("AAA", "sell", 5.0, "market", None, True, False)])

    async def test_partial_credit_orders_only_the_remainder(self):
        exchange, engine, mirror = self.build()
        await engine.process("L", {})
        self.feed(mirror, "buy", 3, start=0)
        await engine.process("L", {"AAA": Position("AAA", 5, mark_price=100)})
        self.assertEqual(exchange.orders,
                         [("AAA", "buy", 2.0, "market", None, False, False)])

    async def test_resync_clears_credits(self):
        exchange, engine, mirror = self.build()
        self.feed(mirror, "buy", 5, start=0)
        self.assertNotEqual(mirror.credits, {})
        mirror.clear_credits()
        self.assertEqual(mirror.credits, {})

    async def test_fills_before_the_floor_are_not_credited(self):
        exchange, engine, mirror = self.build()
        await engine.process("L", {})  # sets previous_at
        floor = time.time()
        mirror._fill_floor["L"] = floor
        old_ms = int((floor - 30) * 1000)
        new_ms = int((floor + 30) * 1000)

        def raw(tid, side, size, start, ms):
            return {"coin": "AAA", "side": side, "px": "95", "sz": str(size),
                    "time": ms, "crossed": False, "startPosition": str(start),
                    "oid": tid, "tid": tid}

        mirror._remember_mirrored("L", "new")
        mirror.on_fills("L", [
            raw("old", "B", 7, 0, old_ms),
            raw("new", "B", 5, 0, new_ms),
        ])
        if mirror._sync_task is not None:
            mirror._sync_task.cancel()
        self.assertEqual(mirror.credits[("L", "AAA")][0], 5.0)

    async def test_startup_cancels_persisted_mirrors_even_when_disabled(self):
        exchange = MirrorExchange()
        engine, mirror = make_engine(exchange, {"enabled": False})
        engine.runtime_state["mirror_orders"] = {
            "L:1": {"order_id": "stale", "symbol": "AAA"},
        }
        await mirror.startup()
        self.assertEqual(exchange.cancelled, ["stale"])
        self.assertEqual(engine.runtime_state["mirror_orders"], {})


class FillBookTests(unittest.TestCase):
    def test_dedupes_and_classifies_entry_vs_exit(self):
        book = LeaderFillBook()
        raw = {
            "coin": "AAA", "side": "B", "px": "95", "sz": "5",
            "time": int(time.time() * 1000), "crossed": False,
            "startPosition": "0", "oid": "1", "tid": "t1",
        }
        first = book.add("L", [raw])
        second = book.add("L", [raw])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertTrue(first[0].is_entry)
        self.assertEqual(first[0].signed_size, 5)
        exit_fill = dict(raw, side="A", startPosition="5", tid="t2", oid="2")
        added = book.add("L", [exit_fill])
        self.assertFalse(added[0].is_entry)
        self.assertEqual(added[0].signed_size, -5)
        self.assertTrue(book.has_oid("L", "1"))


class FakeResponse:
    def __init__(self, data):
        self._data = data

    async def json(self):
        return self._data

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def post(self, url, json=None):
        self.calls.append(json)
        return FakeResponse(self.payloads.get(json.get("type"), {}))


class LeaderOrderFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_filters_triggers_and_reads_marks(self):
        session = FakeSession({
            "frontendOpenOrders": [
                {"coin": "AAA", "side": "B", "limitPx": "95", "sz": "10",
                 "oid": 1, "orderType": "Limit", "isTrigger": False,
                 "reduceOnly": False, "timestamp": 1000},
                {"coin": "BBB", "side": "A", "limitPx": "200", "sz": "4",
                 "oid": 2, "orderType": "Limit", "isTrigger": True,
                 "reduceOnly": True, "timestamp": 1000},
            ],
            "allMids": {"AAA": "100", "BBB": "210"},
        })
        books = await fetch_leader_orders("L", "0xleader", session, dexes=[""])
        orders = books[""]
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].symbol, "AAA")
        self.assertEqual(orders[0].mark, 100.0)
        self.assertAlmostEqual(orders[0].distance, 0.05)

    async def test_fetch_returns_none_when_every_dex_fails(self):
        class Broken:
            def post(self, url, json=None):
                raise RuntimeError("network down")

        self.assertIsNone(
            await fetch_leader_orders("L", "0xleader", Broken(), dexes=[""]))


if __name__ == "__main__":
    unittest.main()
