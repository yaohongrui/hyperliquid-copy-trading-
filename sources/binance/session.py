"""Long-lived Chromium position source used by ``hybrid`` mode.

Authenticated position reads remain inside one persistent Chromium context.
The public configuration name is retained for compatibility with deployments
that previously exported browser cookies to aiohttp.
"""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from time import monotonic

from .browser import BinanceAuthError, BinanceBrowserAuth, BrowserAuthConfig
from .config import BinanceBrowserConfig, BinanceSessionConfig
from .position_source import PollAuthError, PollError, parse_leader_positions

logger = logging.getLogger(__name__)


class HybridBinancePositionSource:
    """Persistent browser context with in-page position polling and recovery."""

    def __init__(self, _legacy_poller, browser_config: BinanceBrowserConfig,
                 session_config: BinanceSessionConfig, fallback_portfolio_id: str,
                 notifier=None):
        # Keep the old constructor shape so existing configuration and factory
        # code continue to work. Its public quote reader supplements the
        # authenticated copy-trading position response.
        self._browser_config = browser_config
        self._market_poller = _legacy_poller
        self._session_config = session_config
        self._probe_portfolio_id = session_config.auth_probe_portfolio_id or fallback_portfolio_id
        self._notifier = notifier
        self._auth: BinanceBrowserAuth | None = None
        self._lock = asyncio.Lock()
        self._next_refresh_at = 0.0
        self._request_count = 0

    def set_notifier(self, notifier) -> None:
        self._notifier = notifier

    def _new_auth(self) -> BinanceBrowserAuth:
        return BinanceBrowserAuth(BrowserAuthConfig(
            profile_dir=Path(self._browser_config.profile_dir),
            headless=self._browser_config.headless,
            timeout_ms=self._browser_config.timeout_ms,
        ))

    def _schedule_refresh(self) -> None:
        base = self._session_config.refresh_interval_hours * 3600
        spread = min(base * .1, 300)
        self._next_refresh_at = monotonic() + max(60, base + random.uniform(-spread, spread))

    async def start(self) -> None:
        async with self._lock:
            start_market = getattr(self._market_poller, "start", None)
            if start_market:
                await start_market()
            try:
                await self._start_browser()
            except Exception:
                stop_market = getattr(self._market_poller, "stop", None)
                if stop_market:
                    await stop_market()
                raise

    async def _start_browser(self) -> None:
        auth = self._new_auth()
        try:
            await auth.start()
            await auth.open_live_session(self._probe_portfolio_id)
            # open_live_session verifies the private account API; empty
            # positions additionally require verified leader visibility.
            await auth.fetch_positions_live(self._probe_portfolio_id)
        except Exception:
            await auth.stop()
            raise
        self._auth = auth
        self._schedule_refresh()
        logger.info(
            "[BrowserLive] persistent session started probe=%s profile=%s",
            self._probe_portfolio_id, self._browser_config.profile_dir,
        )

    async def stop(self) -> None:
        async with self._lock:
            if self._auth is not None:
                await self._auth.stop()
                self._auth = None
            stop_market = getattr(self._market_poller, "stop", None)
            if stop_market:
                await stop_market()
            logger.info("[BrowserLive] persistent session stopped")

    def reload(self, browser_config, session_config, _web_config) -> None:
        if browser_config != self._browser_config:
            raise ValueError("Changing binance_source.browser settings requires restarting the service")
        self._session_config = session_config
        self._probe_portfolio_id = session_config.auth_probe_portfolio_id
        self._schedule_refresh()

    async def _refresh_page(self, reason: str) -> None:
        if self._auth is None:
            raise BinanceAuthError("persistent browser is not started")
        logger.info("[BrowserLive] refreshing page reason=%s", reason)
        await self._auth.reload_live_session()
        await self._auth.fetch_positions_live(self._probe_portfolio_id)
        self._schedule_refresh()
        logger.info("[BrowserLive] page refresh verified probe=%s", self._probe_portfolio_id)

    async def _restart_browser(self, reason: str) -> None:
        logger.warning("[BrowserLive] restarting browser reason=%s", reason)
        if self._auth is not None:
            await self._auth.stop()
            self._auth = None
        await self._start_browser()

    async def fetch_positions(self, portfolio_id: str):
        async with self._lock:
            if self._auth is None:
                raise PollAuthError("persistent browser is not started")
            if monotonic() >= self._next_refresh_at:
                try:
                    await self._refresh_page("scheduled")
                except Exception as exc:
                    try:
                        await self._restart_browser(f"scheduled refresh failed: {exc}")
                    except Exception as restart_exc:
                        raise PollAuthError(
                            f"scheduled browser recovery failed: {restart_exc}"
                        ) from exc
            self._request_count += 1
            request_started = monotonic()
            try:
                items = await self._auth.fetch_positions_live(portfolio_id)
            except Exception as first_error:
                logger.warning(
                    "[BrowserLive] fetch failed portfolio=%s request=%d error=%s",
                    portfolio_id, self._request_count, first_error,
                )
                try:
                    await self._refresh_page("fetch failure")
                    items = await self._auth.fetch_positions_live(portfolio_id)
                except Exception as refresh_error:
                    try:
                        await self._restart_browser(f"page recovery failed: {refresh_error}")
                        items = await self._auth.fetch_positions_live(portfolio_id)
                    except Exception as restart_error:
                        raise PollAuthError(
                            f"persistent browser recovery failed for {portfolio_id}: {restart_error}"
                        ) from first_error
            try:
                positions = parse_leader_positions(items, portfolio_id)
            except PollError:
                raise
            refresh_prices = getattr(self._market_poller, "refresh_current_prices", None)
            if refresh_prices:
                positions = await refresh_prices(positions)
            elapsed_ms = int((monotonic() - request_started) * 1000)
            if not positions:
                logger.info(
                    "[BrowserLive] empty snapshot portfolio=%s request=%d elapsed_ms=%d; "
                    "awaiting confirmation",
                    portfolio_id, self._request_count, elapsed_ms,
                )
            elif self._request_count == 1 or self._request_count % 100 == 0:
                logger.info(
                    "[BrowserLive] heartbeat portfolio=%s request=%d positions=%d "
                    "elapsed_ms=%d",
                    portfolio_id, self._request_count, len(positions), elapsed_ms,
                )
            return positions
