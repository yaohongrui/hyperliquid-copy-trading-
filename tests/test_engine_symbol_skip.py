import unittest

from core.engine import Engine
from core.models import Order, Position


class Exchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self):
        self.orders = []
        self.position_size_calls = []

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        self.position_size_calls.append(symbol)
        return 0

    async def entry_quote(self, symbol, side):
        return 1

    async def create_order(self, symbol, side, amount, order_type, price, reduce_only):
        self.orders.append((symbol, side, amount))
        return Order("1", symbol, side, amount, 1, "closed", reduce_only)


class Notifier:
    def __init__(self):
        self.skipped = []

    async def notify_skipped(self, leader, signal, reason):
        self.skipped.append((leader, signal.symbol, signal.kind, reason))

    async def notify_filled(self, leader, signal, order):
        pass

    async def notify_accepted(self, leader, signal, order):
        pass

    async def notify_failed(self, leader, signal, exc):
        pass


class EngineSymbolSkipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exchange = Exchange()
        self.notifier = Notifier()
        self.leaders = {"L": {"total_margin": 100, "coefficient": 1}}

    def engine(self, mapping):
        return Engine(self.exchange, self.leaders, notifier=self.notifier, symbol_map=mapping)

    async def test_all_signal_kinds_are_skipped_and_notified(self):
        engine = self.engine({"XYZ:SKHX": "skip"})
        await engine.process("L", {})
        for size in (10, 20, 5, 0):
            await engine.process("L", {"XYZ:SKHX": Position("XYZ:SKHX", size, 1)})
        self.assertEqual(self.exchange.orders, [])
        self.assertEqual(self.exchange.position_size_calls, [])
        self.assertEqual([item[2] for item in self.notifier.skipped],
                         ["open", "increase", "decrease", "close"])
        self.assertTrue(all("skip" in item[3] for item in self.notifier.skipped))

    async def test_exact_skip_does_not_skip_plain_symbol(self):
        engine = self.engine({"XYZ:SKHX": "skip", "SKHX": "SKHX/USDT:USDT"})
        await engine.process("L", {})
        await engine.process("L", {
            "XYZ:SKHX": Position("XYZ:SKHX", 1, 1),
            "SKHX": Position("SKHX", 1, 1),
        })
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertEqual(self.exchange.orders[0][0], "SKHX")
        self.assertEqual([item[1] for item in self.notifier.skipped], ["XYZ:SKHX"])

    async def test_skip_clears_pending_carry(self):
        engine = self.engine({"XYZ:SKHX": "skip"})
        engine.small_order_carry["L:XYZ:SKHX"] = {"side": "buy", "amount": 0.004}
        await engine.process("L", {"XYZ:SKHX": Position("XYZ:SKHX", 1, 1)})
        self.assertNotIn("L:XYZ:SKHX", engine.small_order_carry)

    async def test_reconcile_protections_ignores_skipped_owner(self):
        engine = self.engine({"XYZ:SKHX": "skip"})
        engine.owners = {"acct:XYZ:SKHX": "L"}
        await engine.reconcile_protections()
        self.assertEqual(self.exchange.position_size_calls, [])


if __name__ == "__main__":
    unittest.main()
