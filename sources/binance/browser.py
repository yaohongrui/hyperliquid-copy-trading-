"""Standalone Binance browser authentication and read-only health check.

This module deliberately does not import the trading coordinator. It owns only
the persistent browser profile and read-only requests made from that browser.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from time import monotonic
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .config import BinanceBrowserConfig, BinanceWebConfig
from .position_source import PollAuthError, PollError, parse_leader_positions


logger = logging.getLogger(__name__)
# Binance has rotated the web login cookie name on some sessions.  Keep the
# legacy p20t cookie and accept the current r20t cookie as equivalent proof of
# an authenticated browser profile.
LOGIN_COOKIES = ("p20t", "r20t")
UTC8 = timezone(timedelta(hours=8), name="UTC+08:00")
DEFAULT_HOME = "https://www.binance.com/zh-CN"
DEFAULT_LEAD_PAGE = "https://www.binance.com/zh-CN/copy-trading/lead-details/{portfolio_id}"
POSITIONS_URL = (
    "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/"
    "lead-data/positions?portfolioId={portfolio_id}"
)


@dataclass(frozen=True)
class BrowserAuthConfig:
    """Settings for the separate browser profile."""

    profile_dir: Path = Path("data/binance-browser-profile")
    headless: bool = False
    login_url: str = DEFAULT_HOME
    timeout_ms: int = 30_000


class BinanceAuthError(RuntimeError):
    """Browser could not provide a usable Binance session."""


class BinanceBrowserAuth:
    """Manage a persistent Chromium session and read Binance positions."""

    def __init__(self, config: BrowserAuthConfig):
        self.config = config
        self._playwright = None
        self._context = None
        self._page = None
        self.login_cookie_expires_at: float | None = None
        self._live_headers: dict[str, str] = {}

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BinanceAuthError(
                "Playwright is not installed. Run: pip install -r requirements.txt "
                "and then: python -m playwright install chromium"
            ) from exc

        self.config.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.config.profile_dir),
                headless=self.config.headless,
                viewport={"width": 1440, "height": 900},
                locale="zh-CN",
            )
        except Exception as exc:
            await self.stop()
            mode_hint = (
                "Set binance_source.browser.headless to true for non-interactive use."
                if not self.config.headless and not os.environ.get("DISPLAY")
                else "Check that no other Chromium process is using the profile."
            )
            raise BinanceAuthError(f"Could not start Binance browser. {mode_hint}") from exc
        self._context.set_default_timeout(self.config.timeout_ms)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()

    async def stop(self) -> None:
        self._live_headers = {}
        context, playwright = self._context, self._playwright
        self._context = self._page = self._playwright = None
        context_error = None
        if context:
            try:
                await context.close()
            except Exception as exc:
                # Chromium may have already torn down the context while the
                # service is stopping (for example after a browser crash or
                # systemd restart).  Treat that state as already closed, but
                # preserve unexpected cleanup failures for the caller.
                if exc.__class__.__name__ != "TargetClosedError":
                    context_error = exc
        try:
            if playwright:
                await playwright.stop()
        finally:
            if context_error is not None:
                raise context_error

    async def open_login_page(self) -> None:
        self._require_started()
        await self._page.goto(self.config.login_url, wait_until="domcontentloaded")

    async def wait_for_manual_login(self, timeout_seconds: int = 600) -> None:
        """Wait until the user finishes login in the visible browser."""
        self._require_started()
        logger.info("Complete Binance login/verification in the browser window.")
        logger.info("Waiting up to %d seconds; press Ctrl-C to stop.", timeout_seconds)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if await self.is_logged_in():
                logger.info("Binance login session detected and profile saved.")
                return
            await asyncio.sleep(3)
        raise BinanceAuthError("Login was not detected before the timeout")

    async def is_logged_in(self) -> bool:
        self._require_started()
        cookies = await self._context.cookies("https://www.binance.com")
        now = datetime.now(timezone.utc).timestamp()
        for cookie in cookies:
            if str(cookie.get("name", "")).lower() not in LOGIN_COOKIES:
                continue
            if not str(cookie.get("value", "")):
                continue
            # Playwright uses -1 for a session cookie. It is valid while the
            # browser context is alive; persistent cookies must not be past
            # their local expiry before we call them logged in.
            try:
                expires = float(cookie.get("expires", -1))
            except (TypeError, ValueError):
                continue
            if expires > 0 and expires <= now:
                continue
            return True
        return False

    async def _require_logged_in(self) -> None:
        if not await self.is_logged_in():
            raise BinanceAuthError(
                "Binance browser profile has no supported login session (p20t/r20t); "
                "complete login/verification in VNC"
            )

    async def fetch_positions(self, portfolio_id: str) -> list[dict[str, Any]]:
        """Read the positions response generated by the Binance page itself."""
        self._require_started()
        if not portfolio_id:
            raise ValueError("portfolio_id is required")
        lead_url = DEFAULT_LEAD_PAGE.format(portfolio_id=quote(portfolio_id, safe=""))
        responses = []

        def collect_response(response):
            if (
                "/lead-data/positions" in response.url
                and f"portfolioId={portfolio_id}" in response.url
            ):
                responses.append(response)

        self._page.on("response", collect_response)
        try:
            result = None
            for attempt in range(3):
                responses.clear()
                await self._page.goto(lead_url, wait_until="domcontentloaded")
                # Binance may first return a placeholder empty response and
                # issue the authenticated request only after the page settles.
                await self._page.wait_for_timeout(5_000)
                parsed_responses = []
                for response in reversed(responses):
                    try:
                        data = await response.json()
                    except Exception:
                        continue
                    if data.get("code") == "000000" and isinstance(data.get("data"), list):
                        parsed_responses.append((response.status, data))
                        if data["data"]:
                            break
                if parsed_responses:
                    http_status, data = parsed_responses[-1]
                    result = {"httpStatus": http_status, "body": json.dumps(data)}
                    if data["data"] or attempt == 2:
                        break
                if attempt < 2:
                    await asyncio.sleep(2)
            if result is None:
                raise BinanceAuthError("Binance 页面没有返回有效的 positions 数据")
        except Exception as exc:
            if isinstance(exc, BinanceAuthError):
                raise
            raise BinanceAuthError(
                "没有捕获到 Binance 页面发出的 positions 请求；"
                "可能需要重新登录、完成验证，或页面结构已经变化"
            ) from exc
        finally:
            self._page.remove_listener("response", collect_response)
        try:
            data = json.loads(result["body"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise BinanceAuthError(
                f"Binance returned a non-JSON response (HTTP {result.get('httpStatus')})"
            ) from exc
        if data.get("code") != "000000":
            raise BinanceAuthError(
                f"Binance API rejected the browser session: {data.get('message', 'unknown error')}"
            )
        positions = data.get("data")
        if not isinstance(positions, list):
            raise BinanceAuthError("Binance response has an unexpected data format")
        active_positions = []
        for item in positions:
            try:
                amount = Decimal(str(item.get("positionAmount", "0")))
            except (InvalidOperation, TypeError, ValueError):
                raise BinanceAuthError("Binance positionAmount 不是有效数字")
            if amount != 0:
                active_positions.append(item)
        return active_positions

    async def open_live_session(self, portfolio_id: str) -> None:
        """Open one authenticated lead page for subsequent in-page requests."""
        self._require_started()
        await self._require_logged_in()
        lead_url = DEFAULT_LEAD_PAGE.format(portfolio_id=quote(portfolio_id, safe=""))
        started = monotonic()
        await self._navigate_live_session(lead_url)
        logger.info(
            "[BrowserLive] page ready portfolio=%s url=%s elapsed_ms=%d",
            portfolio_id, self._page.url, int((monotonic() - started) * 1000),
        )

    async def reload_live_session(self) -> None:
        """Reload the existing page without replacing the browser context."""
        self._require_started()
        started = monotonic()
        await self._navigate_live_session(self._page.url)
        logger.info(
            "[BrowserLive] page reloaded url=%s elapsed_ms=%d",
            self._page.url, int((monotonic() - started) * 1000),
        )

    async def _navigate_live_session(self, url: str) -> None:
        """Capture session headers from the site's own initialized API client."""
        self._live_headers = {}
        try:
            async with self._page.expect_response(
                lambda response: response.url.split("?")[0] ==
                "https://www.binance.com/bapi/accounts/v1/private/account/get-user-base-info"
                and response.status == 200,
                timeout=self.config.timeout_ms,
            ) as pending:
                await self._page.goto(url, wait_until="domcontentloaded")
            response = await pending.value
            identity = await response.json()
            if identity.get("code") != "000000" or not identity.get("data"):
                raise BinanceAuthError("Page account identity was not verified")
            headers = await response.request.all_headers()
            if not headers.get("csrftoken") or headers.get("clienttype") != "web":
                raise BinanceAuthError("Page authentication headers are incomplete")
            self._live_headers = {
                "csrftoken": headers["csrftoken"], "clienttype": "web",
            }
            await self._verify_live_identity()
        except Exception:
            self._live_headers = {}
            raise BinanceAuthError("Could not establish authenticated browser API session") from None

    async def _verify_live_identity(self) -> None:
        payload = await self._fetch_live_json(
            "https://www.binance.com/bapi/accounts/v1/private/account/get-user-base-info"
        )
        if not isinstance(payload.get("data"), dict) or not payload["data"]:
            raise BinanceAuthError("Browser account identity was not verified")

    async def _verify_position_access(self, portfolio_id: str) -> None:
        await self._verify_live_identity()
        payload = await self._fetch_live_json(
            "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/"
            "lead-portfolio/detail?portfolioId=" + quote(portfolio_id, safe="")
        )
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("positionShow") is not True:
            raise BinanceAuthError("Leader position visibility was not verified")

    async def _fetch_live_json(self, url: str) -> dict[str, Any]:
        self._require_started()
        await self._require_logged_in()
        if not self._live_headers.get("csrftoken") or self._live_headers.get("clienttype") != "web":
            raise BinanceAuthError("Browser API authentication headers are not ready")
        request_timeout_ms = max(5_000, min(self.config.timeout_ms, 15_000))
        try:
            result = await self._page.evaluate(
                """async ({url, timeoutMs, headers}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const response = await fetch(url, {
                            method: 'GET', credentials: 'include', cache: 'no-store',
                            headers: {'Accept': '*/*', ...headers}, signal: controller.signal
                        });
                        return {status: response.status, text: await response.text()};
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                {"url": url, "timeoutMs": request_timeout_ms, "headers": self._live_headers},
            )
        except Exception as exc:
            raise BinanceAuthError("in-page authenticated fetch failed") from None
        try:
            payload = json.loads(result["text"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise BinanceAuthError(
                f"in-page positions returned non-JSON (HTTP {result.get('status')})"
            ) from exc
        if not isinstance(payload, dict):
            raise BinanceAuthError("Browser API returned an unexpected payload")
        if result.get("status") != 200 or payload.get("code") != "000000":
            raise BinanceAuthError(
                "in-page positions rejected: "
                f"HTTP {result.get('status')} code={payload.get('code')} "
            )
        return payload

    async def fetch_positions_live(self, portfolio_id: str) -> list[dict[str, Any]]:
        """Read positions; an empty list alone never proves authentication."""
        started = monotonic()
        payload = await self._fetch_live_json(
            POSITIONS_URL.format(portfolio_id=quote(portfolio_id, safe=""))
        )
        items = payload.get("data")
        if not isinstance(items, list):
            raise BinanceAuthError("in-page positions response has an unexpected data format")
        # Also cover responses containing only zero-size position records.
        if not parse_leader_positions(items, portfolio_id):
            await self._verify_position_access(portfolio_id)
        elapsed_ms = int((monotonic() - started) * 1000)
        logger.debug(
            "[BrowserLive] snapshot portfolio=%s items=%d elapsed_ms=%d",
            portfolio_id, len(items), elapsed_ms,
        )
        return items

    async def refresh_http_auth(
        self, portfolio_id: str, current: BinanceWebConfig
    ) -> BinanceWebConfig:
        """Verify a protected leader page and export current browser auth."""
        self._require_started()
        await self._require_logged_in()
        requests = []

        def collect_request(request):
            if (
                "/lead-data/positions" in request.url
                and f"portfolioId={portfolio_id}" in request.url
            ):
                requests.append(request)

        self._page.on("request", collect_request)
        try:
            await self.fetch_positions(portfolio_id)
            await self._require_logged_in()
            if not requests:
                raise BinanceAuthError("Binance 页面没有发出 positions 请求")
            headers = await requests[-1].all_headers()
            cookies = await self._context.cookies("https://www.binance.com")
        finally:
            self._page.remove_listener("request", collect_request)

        cookie = "; ".join(
            f"{item['name']}={item['value']}" for item in cookies
            if item.get("name") and item.get("value")
        )
        normalized = {name.lower(): value for name, value in headers.items()}
        if not cookie or not any(
            any(item.startswith(f"{name}=") for name in LOGIN_COOKIES)
            for item in cookie.split("; ")
        ):
            raise BinanceAuthError("Browser session did not provide a supported login cookie (p20t/r20t)")
        self.login_cookie_expires_at = self.login_cookie_expiry(cookies)
        self._log_login_cookie_expiry(cookies)
        return BinanceWebConfig(
            cookie=cookie,
            csrf_token=normalized.get("csrftoken", current.csrf_token),
            fvideo_id=normalized.get("fvideo-id", current.fvideo_id),
            fvideo_token=normalized.get("fvideo-token", current.fvideo_token),
            bnc_uuid=normalized.get("bnc-uuid", current.bnc_uuid),
            device_info=normalized.get("device-info", current.device_info),
            user_agent=normalized.get("user-agent", current.user_agent),
        )

    @staticmethod
    def login_cookie_expiry(cookies: list[dict[str, Any]]) -> float | None:
        """Return the supported login cookie's browser expiry, if persistent."""
        for item in cookies:
            if str(item.get("name", "")).lower() not in LOGIN_COOKIES:
                continue
            try:
                expires_at = float(item.get("expires", -1))
            except (TypeError, ValueError):
                return None
            return expires_at if expires_at > 0 else None
        return None

    @classmethod
    def _log_login_cookie_expiry(cls, cookies: list[dict[str, Any]]) -> None:
        """Log login-cookie timing metadata, never the credential value itself."""
        expires_at = cls.login_cookie_expiry(cookies)
        if expires_at is None:
            logger.warning(
                "Binance login cookie is present but has no persistent browser expiry; "
                "its server-side expiry cannot be predicted"
            )
            return

        now = datetime.now(timezone.utc)
        expiry_utc = datetime.fromtimestamp(expires_at, timezone.utc)
        remaining_hours = (expiry_utc - now).total_seconds() / 3600
        level = logging.WARNING if remaining_hours <= 24 else logging.INFO
        logger.log(
            level,
            "Binance login-cookie expiry observed: expires_at_utc=%s expires_at_utc8=%s "
            "remaining_hours=%.1f",
            expiry_utc.isoformat(),
            expiry_utc.astimezone(UTC8).isoformat(),
            remaining_hours,
        )

    def _require_started(self) -> None:
        if not self._page or not self._context:
            raise RuntimeError("Browser auth is not started")


class BinanceBrowserPositionSource:
    """Adapt a persistent Binance browser session to the common poll contract.

    This class owns browser lifecycle only.  It does not import the
    coordinator, Binance Futures client, or any trade-execution code.
    """

    def __init__(self, config: BinanceBrowserConfig):
        self._config = config
        self._auth: BinanceBrowserAuth | None = None
        # The persistent context uses one page to observe network responses.
        # Serialize leader reads so poll and health tasks cannot mix responses.
        self._fetch_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._auth is not None:
            return
        self._auth = BinanceBrowserAuth(BrowserAuthConfig(
            profile_dir=Path(self._config.profile_dir),
            headless=self._config.headless,
            timeout_ms=self._config.timeout_ms,
        ))
        try:
            await self._auth.start()
        except Exception:
            self._auth = None
            raise

    async def stop(self) -> None:
        if self._auth is not None:
            await self._auth.stop()
            self._auth = None

    def reload(self, config: BinanceBrowserConfig) -> None:
        """Accept no-op reloads; browser process settings require restart."""
        if config != self._config:
            raise ValueError(
                "Changing binance_source.browser settings requires restarting the service"
            )

    async def fetch_positions(self, portfolio_id: str):
        if self._auth is None:
            raise RuntimeError("Browser position source not started")
        async with self._fetch_lock:
            try:
                items = await self._auth.fetch_positions(portfolio_id)
                return parse_leader_positions(items, portfolio_id)
            except PollError:
                raise
            except BinanceAuthError as exc:
                # A valid response is the only reliable proof that the persistent
                # profile is still authenticated.  Preserve the coordinator's
                # existing pause-and-notify behaviour when it is not.
                raise PollAuthError(f"Browser Binance session failed: {exc}") from exc
            except Exception as exc:
                raise PollError(f"Browser position request failed for {portfolio_id}: {exc}") from exc


async def _run_login(args: argparse.Namespace) -> int:
    auth = BinanceBrowserAuth(BrowserAuthConfig(Path(args.profile), args.headless, args.url))
    await auth.start()
    try:
        await auth.open_login_page()
        print("请在浏览器窗口中完成 Binance 登录和验证。", flush=True)
        print("程序会自动检测登录状态，最长等待 600 秒。", flush=True)
        await auth.wait_for_manual_login(timeout_seconds=600)
        print("浏览器 profile 已保存。", flush=True)
        return 0
    finally:
        await auth.stop()


async def _run_check(args: argparse.Namespace) -> int:
    auth = BinanceBrowserAuth(BrowserAuthConfig(Path(args.profile), args.headless))
    await auth.start()
    try:
        for portfolio_id in args.portfolio:
            positions = await auth.fetch_positions(portfolio_id)
            print(f"portfolio {portfolio_id}: {len(positions)} active position(s)")
        return 0
    finally:
        await auth.stop()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Binance persistent browser auth (read-only)")
    parser.add_argument("--profile", default=os.environ.get("BINANCE_BROWSER_PROFILE", "data/binance-browser-profile"))
    parser.add_argument("--headless", action="store_true", help="Use only for non-interactive checks")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Open Binance and wait for manual login")
    login.add_argument("--profile", default=argparse.SUPPRESS)
    login.add_argument("--headless", action="store_true", default=argparse.SUPPRESS)
    login.add_argument("--url", default=DEFAULT_HOME)

    check = subparsers.add_parser("check", help="Read positions without trading")
    check.add_argument("--profile", default=argparse.SUPPRESS)
    check.add_argument("--headless", action="store_true", default=argparse.SUPPRESS)
    check.add_argument("portfolio", nargs="+", help="Binance portfolio IDs")

    args = parser.parse_args()
    try:
        result = _run_login(args) if args.command == "login" else _run_check(args)
        raise SystemExit(asyncio.run(result))
    except (BinanceAuthError, KeyboardInterrupt) as exc:
        if isinstance(exc, BinanceAuthError):
            parser.exit(1, f"Error: {exc}\n")
        parser.exit(130, "Stopped.\n")


if __name__ == "__main__":
    cli()
