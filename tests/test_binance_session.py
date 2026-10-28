import unittest
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from sources.binance.session import HybridBinancePositionSource
from sources.binance.config import BinanceBrowserConfig, BinanceSessionConfig
from sources.binance.position_source import PollAuthError


def position(amount="1"):
    return {
        "symbol": "BTCUSDT", "positionSide": "BOTH",
        "positionAmount": amount, "entryPrice": "60000", "leverage": "10",
        "notionalValue": "60000", "markPrice": "60000", "isolated": False,
    }


class FakeAuth:
    instances = []

    def __init__(self, _config):
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.open_live_session = AsyncMock()
        self.reload_live_session = AsyncMock()
        self.fetch_positions_live = AsyncMock(return_value=[position()])
        self.instances.append(self)


class BrowserLiveSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeAuth.instances = []

    def source(self):
        return HybridBinancePositionSource(
            object(), BinanceBrowserConfig(),
            BinanceSessionConfig(refresh_interval_hours=1,
                                 auth_probe_portfolio_id="protected"),
            "fallback",
        )

    async def test_start_keeps_one_browser_and_verifies_protected_probe(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            auth = FakeAuth.instances[0]
            auth.start.assert_awaited_once()
            auth.open_live_session.assert_awaited_once_with("protected")
            auth.fetch_positions_live.assert_awaited_once_with("protected")
            await source.stop()
            auth.stop.assert_awaited_once()

    async def test_fetch_uses_in_page_request_without_page_navigation(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            auth = FakeAuth.instances[0]
            auth.fetch_positions_live.reset_mock()
            result = await source.fetch_positions("leader-1")
            self.assertEqual(len(result), 1)
            auth.fetch_positions_live.assert_awaited_once_with("leader-1")
            auth.reload_live_session.assert_not_awaited()

    async def test_fetch_refreshes_current_price_without_changing_entry_price(self):
        class MarketPoller:
            async def refresh_current_prices(self, positions):
                return [positions[0].__class__(
                    **{**positions[0].__dict__, "mark_price": Decimal("61234.56")}
                )]

        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = HybridBinancePositionSource(
                MarketPoller(), BinanceBrowserConfig(),
                BinanceSessionConfig(refresh_interval_hours=1, auth_probe_portfolio_id="protected"),
                "fallback",
            )
            await source.start()
            result = await source.fetch_positions("leader-1")
            self.assertEqual(result[0].entry_price, Decimal("60000"))
            self.assertEqual(result[0].mark_price, Decimal("61234.56"))
            await source.stop()

    async def test_fetch_failure_reloads_page_then_retries(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            auth = FakeAuth.instances[0]
            auth.fetch_positions_live.reset_mock()
            auth.fetch_positions_live.side_effect = [
                RuntimeError("network"), [position()], [position()]
            ]
            result = await source.fetch_positions("leader-1")
            self.assertEqual(len(result), 1)
            auth.reload_live_session.assert_awaited_once()
            self.assertEqual(auth.fetch_positions_live.await_count, 3)

    async def test_total_recovery_failure_is_never_an_empty_snapshot(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            first = FakeAuth.instances[0]
            first.fetch_positions_live.side_effect = RuntimeError("dead")

            class BrokenAuth(FakeAuth):
                def __init__(self, config):
                    super().__init__(config)
                    self.start.side_effect = RuntimeError("cannot restart")

            with patch("sources.binance.session.BinanceBrowserAuth", BrokenAuth):
                with self.assertRaises(PollAuthError):
                    await source.fetch_positions("leader-1")


if __name__ == "__main__":
    unittest.main()
