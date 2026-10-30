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

from .browser import (
    BinanceAuthError,
    BinanceBrowserAuth,
    BinanceRateLimited,
    BrowserAuthConfig,
)
from .config import BinanceBrowserConfig, BinanceSessionConfig
from .position_source import (
    PollAuthError,
    PollError,
    PollRateLimited,
    parse_leader_positions,
)

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
        # A failed browser restart leaves ``_auth`` empty.  Polls must keep
        # trying to bring it back, with a cooldown so a logged-out profile does
        # not spawn Chromium on every 2.5-second poll.
        self._next_start_at = 0.0
        self._last_start_error: str | None = None
        # Binance throttles the copy-trading endpoint when it is polled every
        # few seconds.  Independent sources may not sustain the shared leader
        # loop cadence, so this one declares its own.
        self._poll_interval = max(1.0, float(session_config.poll_interval_seconds))
        self._request_count = 0

    @property
    def poll_interval(self) -> float:
        return self._poll_interval

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
        except Exception as exc:
            await auth.stop()
            self._last_start_error = str(exc)
            raise
        self._auth = auth
        self._next_start_at = 0.0
        self._last_start_error = None
        self._schedule_refresh()
        logger.info(
            "[BrowserLive] persistent session started probe=%s profile=%s",
            self._probe_portfolio_id, self._browser_config.profile_dir,
        )

    async def _ensure_browser(self) -> None:
        """Start a browser that a previous recovery attempt left dead."""
        if self._auth is not None:
            return
        now = monotonic()
        if now < self._next_start_at:
            remaining = max(1, int(self._next_start_at - now + 0.999))
            detail = f"; last error: {self._last_start_error}" if self._last_start_error else ""
            raise PollAuthError(
                f"persistent browser is not started (retrying in {remaining}s{detail})",
                retry_after=float(remaining),
            )
        try:
            await self._start_browser()
        except BinanceRateLimited as exc:
            self._next_start_at = monotonic() + 60
            raise self._rate_limited(exc) from exc
        except Exception as exc:
            self._next_start_at = monotonic() + 60
            raise PollAuthError(
                f"persistent browser restart failed: {exc}", retry_after=60.0
            ) from exc

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
        self._poll_interval = max(1.0, float(session_config.poll_interval_seconds))
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
        auth, self._auth = self._auth, None
        if auth is not None:
            try:
                await auth.stop()
            except Exception:
                logger.warning("[BrowserLive] failed to stop dead browser", exc_info=True)
        try:
            await self._start_browser()
        except Exception:
            self._next_start_at = monotonic() + 60
            raise

    def _rate_limited(self, exc: BinanceRateLimited) -> PollRateLimited:
        """Turn Binance's throttle into a back-off instead of a session error.

        Reloading the page or restarting Chromium would only add more requests
        against the same throttled account, which extends the throttle.
        """
        logger.warning("[BrowserLive] Binance throttled position reads: %s", exc)
        return PollRateLimited(str(exc), retry_after=exc.retry_after)

    async def fetch_positions(self, portfolio_id: str):
        async with self._lock:
            await self._ensure_browser()
            if monotonic() >= self._next_refresh_at:
                try:
                    await self._refresh_page("scheduled")
                except BinanceRateLimited as exc:
                    raise self._rate_limited(exc) from exc
                except Exception as exc:
                    try:
                        await self._restart_browser(f"scheduled refresh failed: {exc}")
                    except BinanceRateLimited as restart_exc:
                        raise self._rate_limited(restart_exc) from restart_exc
                    except Exception as restart_exc:
                        raise PollAuthError(
                            f"scheduled browser recovery failed: {restart_exc}"
                        ) from exc
            self._request_count += 1
            request_started = monotonic()
            try:
                items = await self._auth.fetch_positions_live(portfolio_id)
            except BinanceRateLimited as exc:
                raise self._rate_limited(exc) from exc
            except Exception as first_error:
                logger.warning(
                    "[BrowserLive] fetch failed portfolio=%s request=%d error=%s",
                    portfolio_id, self._request_count, first_error,
                )
                try:
                    await self._refresh_page("fetch failure")
                    items = await self._auth.fetch_positions_live(portfolio_id)
                except BinanceRateLimited as exc:
                    raise self._rate_limited(exc) from exc
                except Exception as refresh_error:
                    try:
                        await self._restart_browser(f"page recovery failed: {refresh_error}")
                        items = await self._auth.fetch_positions_live(portfolio_id)
                    except BinanceRateLimited as exc:
                        raise self._rate_limited(exc) from exc
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
