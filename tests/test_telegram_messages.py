import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.models import Order
from telegram.bot import TelegramManager


def signal(kind="open", side="long", delta=100, price=10.0):
    return SimpleNamespace(
        symbol="ESPORTS", kind=kind, side=side, delta=delta, price=price,
    )


class TelegramMessageTests(unittest.IsolatedAsyncioTestCase):
    def manager(self):
        manager = TelegramManager(None, "token", "chat", account_name="gate_primary")
        manager.send = AsyncMock(return_value=True)
        return manager

    async def sent_text(self, manager, coroutine):
        await coroutine
        return manager.send.await_args.args[0]

    async def test_resting_order_says_explicitly_that_it_is_unfilled(self):
        manager = self.manager()
        order = Order("123", "ESPORTS", "buy", 200, 1.5, "open", False)

        text = await self.sent_text(
            manager,
            manager.notify_accepted("binance-1", signal(), order, order_type="limit"),
        )

        self.assertIn("ORDER RESTING (UNFILLED)", text)
        self.assertIn("<b>Account</b>: gate_primary", text)
        self.assertIn("<b>Leader position</b>: LONG", text)
        self.assertIn("<b>Order side</b>: BUY", text)
        self.assertIn("<b>Signal</b>: OPEN", text)
        self.assertIn("<b>Order type</b>: LIMIT", text)
        self.assertIn("<b>Order status</b>: OPEN — resting, not filled", text)
        self.assertIn("<b>Limit price</b>: 1.5", text)
        self.assertIn("<b>Market at signal</b>: 10", text)
        self.assertIn("<b>Note</b>", text)
        self.assertNotIn("<b>Action</b>", text)

    async def test_filled_order_reports_fill_price_not_limit_price(self):
        manager = self.manager()
        order = Order("123", "ESPORTS", "buy", 200, 1.5, "closed", False)

        text = await self.sent_text(
            manager,
            manager.notify_filled("binance-1", signal(), order, order_type="market"),
        )

        self.assertIn("ORDER FILLED", text)
        self.assertIn("<b>Order type</b>: MARKET", text)
        self.assertIn("<b>Filled qty</b>: 200", text)
        self.assertIn("<b>Avg fill price</b>: 1.5", text)
        self.assertNotIn("Limit price", text)

    async def test_skipped_message_is_about_a_signal_not_an_order(self):
        manager = self.manager()

        text = await self.sent_text(
            manager,
            manager.notify_skipped("binance-1", signal(), "symbol is blacklisted"),
        )

        self.assertIn("SIGNAL SKIPPED", text)
        self.assertIn("<b>Reason</b>: symbol is blacklisted", text)

    async def test_failed_message_labels_the_error(self):
        manager = self.manager()

        text = await self.sent_text(
            manager,
            manager.notify_failed("binance-1", signal(), RuntimeError("boom")),
        )

        self.assertIn("ORDER FAILED", text)
        self.assertIn("<b>Error</b>: boom", text)

    async def test_cancelled_message_carries_the_cancel_reason(self):
        manager = self.manager()
        entry = {"id": "123", "side": "buy", "amount": 200.0}

        text = await self.sent_text(
            manager,
            manager.notify_cancelled("binance-1", "ESPORTS", entry, "leader is flat"),
        )

        self.assertIn("UNFILLED ORDER CANCELLED", text)
        self.assertIn("<b>Order side</b>: BUY", text)
        self.assertIn("<b>Quantity</b>: 200", text)
        self.assertIn("<b>Order ID</b>: 123", text)
        self.assertIn("<b>Reason</b>: leader is flat", text)

    async def test_hard_stop_does_not_claim_the_flatten_already_succeeded(self):
        manager = self.manager()

        text = await self.sent_text(manager, manager.notify_hard_stop(80.0, 100.0, 20.0))

        self.assertNotIn("All positions closed", text)
        self.assertIn("being flattened", text)
        self.assertIn("<b>Next step</b>", text)


if __name__ == "__main__":
    unittest.main()
