"""The source-failure notification must name the failing leader."""

import unittest
from unittest.mock import AsyncMock

from telegram.bot import TelegramManager


class NotifySourceFailedTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_contains_title_and_leader(self):
        manager = TelegramManager.__new__(TelegramManager)
        manager.send = AsyncMock()
        error = RuntimeError("polling failed")

        await manager.notify_source_failed("Binance Leader", error)

        manager.send.assert_awaited_once()
        message = manager.send.await_args.args[0]
        self.assertIn("SOURCE FAILED", message)
        self.assertIn("Binance Leader", message)
        self.assertIn("polling failed", message)


if __name__ == "__main__":
    unittest.main()
