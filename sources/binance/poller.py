import logging
import re
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from time import time

import aiohttp

from .config import BinanceWebConfig
from .position_source import LeaderPosition, PollAuthError, PollError, parse_leader_positions

logger = logging.getLogger(__name__)

BASE_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-data/positions"
FUTURES_TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/price"
COOKIE_EXPIRY_RE = re.compile(r"(?:^|;)\s*BNC_FV_KEY_EXPIRE=([^;]+)")
REQUIRED_COOKIE_FIELDS = ("p20t",)
SUPPORTED_LOGIN_COOKIE_FIELDS = ("p20t", "r20t")


def cookie_expiry_ms(cookie: str) -> int | None:
    """Return the browser fingerprint cookie expiry timestamp, if present."""
    match = COOKIE_EXPIRY_RE.search(cookie or "")
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def cookie_expired(cookie: str, now_ms: int | None = None) -> bool:
    expiry_ms = cookie_expiry_ms(cookie)
    return expiry_ms is not None and expiry_ms < (now_ms or int(time() * 1000))


def missing_cookie_fields(cookie: str) -> list[str]:
    fields = {
        part.strip().split("=", 1)[0].lower()
        for part in (cookie or "").split(";")
        if "=" in part
    }
    # p20t is the legacy name; Binance currently issues r20t for some web
    # sessions.  Either one is sufficient to authenticate the request.
    if any(field in fields for field in SUPPORTED_LOGIN_COOKIE_FIELDS):
        return []
    # Keep the legacy diagnostic/API shape for callers while accepting r20t.
    return list(REQUIRED_COOKIE_FIELDS)


def cookie_value(cookie: str, name: str) -> str:
    target = name.lower()
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() == target:
            return value
    return ""


def _build_headers(cfg: BinanceWebConfig, portfolio_id: str) -> dict[str, str]:
    return {
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "BNC-Location": "CN",
        "BNC-Time-Zone": "Asia/Shanghai",
        "BNC-Level": "0",
        "BNC-UUID": cfg.bnc_uuid or cookie_value(cfg.cookie, "bnc-uuid"),
        "Cache-Control": "no-cache",
        "clienttype": "web",
        "Content-Type": "application/json",
        "csrftoken": cfg.csrf_token,
        "Device-Info": cfg.device_info,
        "FVIDEO-ID": cfg.fvideo_id or cookie_value(cfg.cookie, "BNC_FV_KEY"),
        "FVIDEO-Token": cfg.fvideo_token,
        "Lang": "zh-CN",
        "Pragma": "no-cache",
        "Referer": f"https://www.binance.com/zh-CN/copy-trading/lead-details/{portfolio_id}",
        "User-Agent": cfg.user_agent,
        "Cookie": cfg.cookie,
    }


class Poller:
    def __init__(self, cfg: BinanceWebConfig):
        self._cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    def reload(self, cfg: BinanceWebConfig):
        """热重载：更新 Cookie/CSRF 等请求头配置。"""
        self._cfg = cfg
        logger.info("Poller config reloaded")

    @property
    def auth_config(self) -> BinanceWebConfig:
        """Return the current in-memory HTTP authentication settings."""
        return self._cfg

    async def refresh_current_prices(
        self, positions: list[LeaderPosition]
    ) -> list[LeaderPosition]:
        """Replace snapshot marks with Binance Futures' latest traded prices.

        The copy-trading position endpoint can return a stale ``markPrice``
        (and occasionally echoes the entry price).  This public endpoint is
        deliberately best-effort: a quote outage must never turn a valid
        position snapshot into a failed/empty source read.
        """
        if not positions or not self._session:
            return positions
        symbols = sorted({position.symbol for position in positions})
        try:
            quotes = []
            for symbol in symbols:
                async with self._session.get(
                    FUTURES_TICKER_URL,
                    params={"symbol": symbol},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as response:
                    if response.status != 200:
                        continue
                    quote = await response.json(content_type=None)
                    if isinstance(quote, dict):
                        quotes.append(quote)
            prices = {
                str(quote.get("symbol")): Decimal(str(quote["price"]))
                for quote in quotes
                if quote.get("symbol") and quote.get("price") not in (None, "")
            }
        except (aiohttp.ClientError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
            logger.warning("Binance public current-price refresh failed: %s", exc)
            return positions
        return [
            replace(position, mark_price=prices.get(position.symbol, position.mark_price))
            for position in positions
        ]

    async def fetch_positions(self, portfolio_id: str) -> list[LeaderPosition]:
        """Fetch leader positions.

        成功返回位置列表（可能为空）；失败抛出异常：
          - PollAuthError: Cookie 失效/鉴权失败
          - PollError: 网络或数据解析错误

        注意：网络错误绝不能返回空列表，否则会被当成"带单员已清仓"
        而触发批量平仓。
        """
        if not self._session:
            raise RuntimeError("Poller not started")

        # Do not send a request with a known-expired browser fingerprint.  In
        # practice Binance can answer that request with HTTP 200 and an empty
        # data list, which is not evidence that the leader actually closed.
        if cookie_expired(self._cfg.cookie):
            raise PollAuthError(
                f"HTTP authentication cookie BNC_FV_KEY_EXPIRE has expired for {portfolio_id}"
            )

        headers = _build_headers(self._cfg, portfolio_id)
        url = f"{BASE_URL}?portfolioId={portfolio_id}"

        try:
            async with self._session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        except Exception as e:
            logger.error("Poll failed for %s: %s", portfolio_id, e)
            raise PollError(f"Request failed for {portfolio_id}: {e}") from e

        if data.get("code") != "000000":
            logger.error("API error for %s: %s", portfolio_id, data.get("message"))
            raise PollAuthError(f"API error for {portfolio_id}: {data.get('message')}")

        try:
            positions = parse_leader_positions(data.get("data"), portfolio_id)
            return await self.refresh_current_prices(positions)
        except PollError as e:
            logger.error("Failed to parse positions for %s: %s", portfolio_id, e)
            raise
