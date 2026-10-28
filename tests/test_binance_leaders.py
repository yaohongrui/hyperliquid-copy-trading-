import unittest
from decimal import Decimal

from sources.binance.leaders import (
    LeaderPoller,
    base_symbol,
    signed_amount,
    to_positions,
)
from sources.binance.position_source import LeaderPosition


def row(symbol="BTCUSDT", side="BOTH", amount="1", entry="100", mark="101", pnl="1", leverage=10):
    return LeaderPosition(
        symbol=symbol,
        position_side=side,
        position_amount=Decimal(amount),
        entry_price=Decimal(entry),
        leverage=leverage,
        notional_value=Decimal("100"),
        mark_price=Decimal(mark),
        isolated=False,
        unrealised_pnl=Decimal(pnl),
    )


class ConversionTests(unittest.TestCase):
    def test_base_symbol_strips_quote(self):
        self.assertEqual(base_symbol("BTCUSDT"), "BTC")
        self.assertEqual(base_symbol("1000PEPEUSDT"), "1000PEPE")
        self.assertEqual(base_symbol("ethusdc"), "ETH")
        self.assertEqual(base_symbol("btc"), "BTC")

    def test_signed_amount_by_position_side(self):
        self.assertEqual(signed_amount(row(side="LONG", amount="2")), Decimal("2"))
        self.assertEqual(signed_amount(row(side="SHORT", amount="2")), Decimal("-2"))
        self.assertEqual(signed_amount(row(side="BOTH", amount="-2")), Decimal("-2"))
        self.assertEqual(signed_amount(row(side="BOTH", amount="2")), Decimal("2"))

    def test_to_positions_nets_hedge_rows(self):
        positions = to_positions([
            row(side="LONG", amount="1", entry="100", pnl="2"),
            row(side="SHORT", amount="0.4", entry="110", pnl="-1"),
        ])
        self.assertEqual(set(positions), {"BTC"})
        btc = positions["BTC"]
        self.assertAlmostEqual(btc.size, 0.6)
        self.assertEqual(btc.entry_price, 100)
        self.assertAlmostEqual(btc.unrealized_pnl, 1)

    def test_to_positions_drops_zero_rows(self):
        self.assertEqual(to_positions([row(amount="0")]), {})
        self.assertEqual(to_positions([row(amount="0.000")]), {})

    def test_to_positions_keeps_short_sign(self):
        positions = to_positions([row(side="SHORT", amount="2")])
        self.assertEqual(positions["BTC"].size, -2)


class FakeSource:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def fetch_positions(self, portfolio_id):
        self.calls += 1
        item = self.responses.pop(0) if self.responses else []
        if isinstance(item, Exception):
            raise item
        return item


class FakeEngine:
    def __init__(self):
        self.calls = []

    async def process(self, name, positions):
        self.calls.append((name, dict(positions)))


class FakeNotifier:
    def __init__(self):
        self.calls = []

    async def notify_source_failed(self, name, exc):
        self.calls.append((name, exc))


class PollerTests(unittest.IsolatedAsyncioTestCase):
    def poller(self, source, notifier=None, leader=None):
        engine = FakeEngine()
        instance = LeaderPoller(
            engine,
            {"s": source},
            {"L": leader or {"source": "s", "portfolio_id": "1"}},
            {"interval_seconds": 0.01, "jitter_ms": 0},
            notifier=notifier,
        )
        return instance, engine

    async def test_missing_row_is_retained_once_then_closed(self):
        source = FakeSource([[row()], [], []])
        poller, engine = self.poller(source)

        await poller.poll_once("L")
        self.assertIn("BTC", poller.positions("L"))
        self.assertEqual(len(poller.raw_positions("L")), 1)
        await poller.poll_once("L")
        self.assertIn("BTC", poller.positions("L"))
        self.assertEqual(len(poller.raw_positions("L")), 1)
        await poller.poll_once("L")
        self.assertEqual(poller.positions("L"), {})
        self.assertEqual(poller.raw_positions("L"), [])

        self.assertEqual(len(engine.calls), 3)
        self.assertEqual(engine.calls[2][1], {})

    async def test_source_failure_is_not_an_empty_snapshot(self):
        source = FakeSource([[row()], RuntimeError("boom")])
        poller, engine = self.poller(source)

        await poller.poll_once("L")
        before = dict(poller.positions("L"))
        await poller.poll_once("L")

        self.assertEqual(poller.positions("L"), before)
        self.assertEqual(poller.errors["L"], 1)
        self.assertEqual(len(engine.calls), 1)

    async def test_success_resets_error_counter(self):
        source = FakeSource([RuntimeError("boom"), [row()], RuntimeError("again")])
        poller, engine = self.poller(source)

        await poller.poll_once("L")
        self.assertEqual(poller.errors["L"], 1)
        await poller.poll_once("L")
        self.assertEqual(poller.errors["L"], 0)
        await poller.poll_once("L")
        self.assertEqual(poller.errors["L"], 1)

    async def test_disabled_leader_is_skipped_and_forgotten(self):
        source = FakeSource([[row()]])
        poller, engine = self.poller(
            source, leader={"source": "s", "portfolio_id": "1", "enabled": False}
        )
        # Seed every cache store as if the leader had been polled before being
        # disabled.
        poller.latest["L"] = {"BTC": object()}
        poller.latest_raw["L"] = [row()]
        poller.missing["L"] = {"BTC": 1}
        poller.errors["L"] = 4
        poller.last_success["L"] = 123.0

        await poller.poll_once("L")

        self.assertEqual(source.calls, 0)
        self.assertEqual(engine.calls, [])
        for store in (poller.latest, poller.latest_raw, poller.missing,
                      poller.errors, poller.last_success):
            self.assertNotIn("L", store)
        self.assertIsNone(poller.positions("L"))
        self.assertIsNone(poller.raw_positions("L"))

    async def test_error_counter_increments_while_logging_is_throttled(self):
        source = FakeSource([RuntimeError("boom")] * 20)
        poller, engine = self.poller(source)

        with self.assertLogs("sources.binance.leaders", level="WARNING") as captured:
            for _ in range(20):
                await poller.poll_once("L")

        # Every attempt counts, but only attempts 1, 3 and 20 log a warning.
        self.assertEqual(poller.errors["L"], 20)
        self.assertEqual(len(captured.records), 3)
        self.assertEqual(engine.calls, [])

    async def test_notification_fires_exactly_on_third_consecutive_error(self):
        notifier = FakeNotifier()
        source = FakeSource([RuntimeError("boom")] * 5)
        poller, engine = self.poller(source, notifier=notifier)

        for _ in range(2):
            await poller.poll_once("L")
        self.assertEqual(notifier.calls, [])

        await poller.poll_once("L")
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][0], "L")

        # A fourth consecutive failure does not re-notify.
        await poller.poll_once("L")
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(poller.errors["L"], 4)

    async def test_success_resets_counter_and_rearms_notification(self):
        notifier = FakeNotifier()
        source = FakeSource([
            RuntimeError("a"), RuntimeError("b"), [row()],
            RuntimeError("c"), RuntimeError("d"), RuntimeError("e"),
        ])
        poller, engine = self.poller(source, notifier=notifier)

        await poller.poll_once("L")
        await poller.poll_once("L")
        self.assertEqual(poller.errors["L"], 2)
        self.assertEqual(notifier.calls, [])

        await poller.poll_once("L")
        self.assertEqual(poller.errors["L"], 0)
        self.assertEqual(notifier.calls, [])

        for _ in range(3):
            await poller.poll_once("L")
        self.assertEqual(poller.errors["L"], 3)
        self.assertEqual(len(notifier.calls), 1)


if __name__ == "__main__":
    unittest.main()
