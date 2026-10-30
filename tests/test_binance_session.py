import unittest
from time import monotonic
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from sources.binance.session import HybridBinancePositionSource
from sources.binance.config import BinanceBrowserConfig, BinanceSessionConfig
from sources.binance.browser import BinanceRateLimited
from sources.binance.position_source import PollAuthError, PollRateLimited


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

            self.assertIsNone(source._auth)
            self.assertGreater(source._next_start_at, 0)

    async def test_a_later_poll_restarts_the_dead_browser(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            FakeAuth.instances[0].fetch_positions_live.side_effect = RuntimeError("dead")

            class BrokenAuth(FakeAuth):
                def __init__(self, config):
                    super().__init__(config)
                    self.start.side_effect = RuntimeError("cannot restart")

            with patch("sources.binance.session.BinanceBrowserAuth", BrokenAuth):
                with self.assertRaises(PollAuthError):
                    await source.fetch_positions("leader-1")

            # The cooldown has elapsed: the next poll must bring the browser
            # back instead of failing with "not started" forever.
            source._next_start_at = 0
            result = await source.fetch_positions("leader-1")

            self.assertEqual(len(result), 1)
            self.assertIsNotNone(source._auth)
            self.assertEqual(len(FakeAuth.instances), 3)  # original + broken + recovered
            await source.stop()

    async def test_failed_restart_is_throttled_until_the_cooldown_ends(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            FakeAuth.instances[0].fetch_positions_live.side_effect = RuntimeError("dead")

            class BrokenAuth(FakeAuth):
                def __init__(self, config):
                    super().__init__(config)
                    self.start.side_effect = RuntimeError("cannot restart")

            with patch("sources.binance.session.BinanceBrowserAuth", BrokenAuth):
                with self.assertRaises(PollAuthError):
                    await source.fetch_positions("leader-1")
                attempts = len(FakeAuth.instances)
                with self.assertRaises(PollAuthError):
                    await source.fetch_positions("leader-1")
                self.assertEqual(len(FakeAuth.instances), attempts)

    async def test_poll_starts_a_browser_that_was_never_running(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            self.assertIsNone(source._auth)

            result = await source.fetch_positions("leader-1")

            self.assertEqual(len(result), 1)
            auth = FakeAuth.instances[0]
            auth.open_live_session.assert_awaited_once_with("protected")
            await source.stop()

    async def test_poll_interval_comes_from_session_config(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = HybridBinancePositionSource(
                object(), BinanceBrowserConfig(),
                BinanceSessionConfig(auth_probe_portfolio_id="protected",
                                     poll_interval_seconds=7.5),
                "fallback",
            )
            self.assertEqual(source.poll_interval, 7.5)

    async def test_throttled_fetch_backs_off_without_touching_the_browser(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            auth = FakeAuth.instances[0]
            auth.fetch_positions_live.reset_mock()
            auth.fetch_positions_live.side_effect = BinanceRateLimited("请求过于频繁")

            with self.assertRaises(PollRateLimited) as captured:
                await source.fetch_positions("leader-1")

            self.assertEqual(captured.exception.retry_after, 60.0)
            auth.reload_live_session.assert_not_awaited()
            self.assertIs(source._auth, auth)
            self.assertEqual(len(FakeAuth.instances), 1)  # no restart
            await source.stop()

    async def test_throttled_page_refresh_does_not_restart_the_browser(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            await source.start()
            auth = FakeAuth.instances[0]
            source._next_refresh_at = 0
            auth.reload_live_session.side_effect = BinanceRateLimited("请求过于频繁")

            with self.assertRaises(PollRateLimited):
                await source.fetch_positions("leader-1")

            self.assertIs(source._auth, auth)
            self.assertEqual(len(FakeAuth.instances), 1)  # no restart
            await source.stop()

    async def test_cooldown_error_reports_the_last_failure_and_retry_after(self):
        with patch("sources.binance.session.BinanceBrowserAuth", FakeAuth):
            source = self.source()
            source._next_start_at = monotonic() + 60
            source._last_start_error = "login cookie expired"

            with self.assertRaises(PollAuthError) as captured:
                await source.fetch_positions("leader-1")

            self.assertIn("login cookie expired", str(captured.exception))
            self.assertGreater(captured.exception.retry_after, 0)


if __name__ == "__main__":
    unittest.main()
