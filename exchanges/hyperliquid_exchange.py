"""Hyperliquid execution adapter.

Executes orders on the same venue the leaders trade on, so coin names need no
venue bridging.  The official SDK is synchronous, so every network call is run
in a worker thread to keep the engine's event loop responsive.

HIP-3 markets live on builder-deployed perp dexes.  Their assets are named
``DEX:COIN`` (e.g. ``XYZ:AMD``) and are only registered by the SDK when the
list of dex names is passed in; ``_connect`` discovers that list from
``perpDexs`` and enables every one of them.
"""

import asyncio
import functools
import logging
import threading
import time

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL, TESTNET_API_URL
from hyperliquid.utils.error import ClientError

from core.models import Order
from core.request_stats import record as record_request
from core.symbols import SymbolMap
from .base import OrderGone, OrderQuantityTooSmall

log = logging.getLogger(__name__)

_SMALL_ORDER_TOKENS = (
    "minimum", "too small", "notional", "minimum value", "below the minimum",
)
_ORDER_GONE_TOKENS = (
    "ORDER_NOT_FOUND", "ORDER NOT FOUND", "NOT_FOUND", "NOT FOUND",
    "UNKNOWN_ORDER", "UNKNOWN ORDER", "ALREADY FILLED", "ALREADY CLOSED",
    "ALREADY CANCELED", "ALREADY CANCELLED", "NEVER PLACED",
    "NOT OPEN", "DOES NOT EXIST", "NO SUCH ORDER",
)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _address(value):
    # PyYAML may parse an unquoted 0x address as an int.
    return hex(value) if isinstance(value, int) else (str(value) if value else None)


def _small_order(message):
    lowered = str(message).lower()
    return any(token in lowered for token in _SMALL_ORDER_TOKENS)


def _order_gone(exc):
    if isinstance(exc, OrderGone):
        return True
    message = str(exc).upper()
    return any(token in message for token in _ORDER_GONE_TOKENS)


def _serialized(method):
    """Serialize a synchronous SDK call on the adapter's re-entrant lock.

    ``requests.Session`` is not safe to use from several threads at once, and
    the Console refreshes performance data while the engine trades.  Every
    blocking SDK entry point therefore takes the same lock; it is an ``RLock``
    so nested helpers (e.g. cancel -> open orders) can re-enter.
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class HyperliquidExchange:
    """Minimal order/position adapter backed by the official Hyperliquid SDK."""

    def __init__(self, config, symbol_map=None):
        self.name = "hyperliquid"
        self.config = config or {}
        self.symbol_map = symbol_map if isinstance(symbol_map, SymbolMap) else SymbolMap(symbol_map)
        self.base_url = TESTNET_API_URL if self.config.get("testnet") else MAINNET_API_URL
        self.account_address = _address(self.config.get("account_address"))
        self.private_key = self.config.get("api_private_key") or self.config.get("secret_key")
        self.slippage = float(self.config.get("slippage", 0.05) or 0.05)
        self.request_timeout = float(self.config.get("request_timeout_seconds", 10) or 10)
        self.performance_page_size = int(self.config.get("performance_page_size", 2000) or 2000)
        flow_types = (
            self.config.get("cash_flow_types")
            or self.config.get("performance_cash_flow_types")
            or ("deposit", "withdraw", "send", "receive", "internalTransfer",
                "spotTransfer", "vaultDeposit", "vaultWithdraw")
        )
        if isinstance(flow_types, str):
            flow_types = (flow_types,)
        self.cash_flow_types = tuple(str(item).lower() for item in flow_types)
        self._lock = threading.RLock()
        # Console and engine both poll account state.  A very short cache lets
        # fetch_positions/equity/open-orders share one round of per-dex calls
        # without returning noticeably stale data; mutations clear it.
        self._state_cache = {}
        self._orders_cache = {}
        self._cache_ttl = max(0.0, float(self.config.get("state_cache_seconds", 2.0) or 0))
        self.info = None
        self.client = None
        self.address = None
        self.dexes = [""]
        self._coins = {}
        self._max_leverage = {}
        # Unified accounts treat spot USDC as perp collateral, so equity must
        # include the free spot balance in addition to the perp account value.
        self.unified = False
        # update_leverage is a signed request; only resend when the cache misses.
        self._leverage_set = {}

    # -- lifecycle ---------------------------------------------------------
    async def start(self):
        await asyncio.to_thread(self._connect)

    async def stop(self):
        # The SDK uses plain HTTP requests (no background websocket) when
        # ``skip_ws=True``, so there is nothing to tear down.
        return None

    @staticmethod
    def _is_transient(exc):
        return getattr(exc, "status_code", None) in (429, 500, 502, 503, 504)

    def _sdk_call(self, fn, *args, **kwargs):
        """Run one synchronous SDK call with a small backoff on rate limits."""
        record_request(getattr(fn, "__name__", "sdk"))
        delay = 1.0
        for attempt in range(3):
            try:
                return fn(*args, **kwargs)
            except ClientError as exc:
                if not self._is_transient(exc) or attempt == 2:
                    raise
                log.warning("Hyperliquid request throttled; retrying in %.1fs", delay)
                time.sleep(delay)
                delay *= 2

    def _cache_get(self, cache, key):
        item = cache.get(key)
        if item is None:
            return None
        at, value = item
        if time.monotonic() - at <= self._cache_ttl:
            return value
        cache.pop(key, None)
        return None

    @staticmethod
    def _cache_put(cache, key, value):
        cache[key] = (time.monotonic(), value)
        return value

    def _invalidate_account_cache(self):
        self._state_cache.clear()
        self._orders_cache.clear()

    def _user_state(self, dex):
        cached = self._cache_get(self._state_cache, dex)
        if cached is not None:
            return cached
        return self._cache_put(
            self._state_cache, dex, self._sdk_call(self.info.user_state, self.address, dex))

    @_serialized
    def _connect(self):
        if not self.private_key:
            raise RuntimeError("api_private_key is required for Hyperliquid execution")
        wallet = Account.from_key(self.private_key)
        self.address = self.account_address or wallet.address

        # The default Info only knows the original dex; ask for the full list
        # of builder-deployed dexes so their assets are registered too.
        bootstrap = self._sdk_call(
            Info, self.base_url, skip_ws=True, timeout=self.request_timeout)
        raw = self._sdk_call(bootstrap.perp_dexs) or []
        hip3 = [item["name"] for item in raw[1:] if item and item.get("name")]
        self.dexes = [""] + hip3

        self.info = self._sdk_call(
            Info,
            self.base_url, skip_ws=True, perp_dexs=self.dexes, timeout=self.request_timeout,
        )
        self.client = self._sdk_call(
            Exchange,
            wallet,
            base_url=self.base_url,
            account_address=self.account_address,
            perp_dexs=self.dexes,
            timeout=self.request_timeout,
        )
        self._coins = {name.upper(): name for name in self.info.coin_to_asset}
        try:
            abstraction = self._sdk_call(
                self.info.query_user_abstraction_state, self.address)
            self.unified = abstraction in ("unifiedAccount", "portfolioMargin")
        except Exception:
            log.warning("Failed to read Hyperliquid account abstraction", exc_info=True)
        self._max_leverage = {}
        for dex in self.dexes:
            try:
                meta = self._sdk_call(
                    (lambda d=dex: self.info.meta(dex=d)) if dex else self.info.meta)
            except Exception:
                log.warning("Failed to read Hyperliquid meta for dex %s", dex or "default", exc_info=True)
                continue
            for item in meta.get("universe") or []:
                if item.get("name"):
                    self._max_leverage[item["name"].upper()] = int(item.get("maxLeverage") or 0)
        log.info(
            "Hyperliquid execution account=%s dexes=%d coins=%d",
            self.address, len(self.dexes), len(self._coins),
        )

    # -- symbol / precision helpers ---------------------------------------
    def _coin(self, symbol):
        raw = str(symbol or "").strip()
        if raw.upper() in self._coins:
            return self._coins[raw.upper()]
        if raw in self.info.coin_to_asset:
            return raw
        target = self.symbol_map.target(symbol)
        if target:
            if target.upper() in self._coins:
                return self._coins[target.upper()]
            if target in self.info.coin_to_asset:
                return target
        raise ValueError(f"No Hyperliquid market for {symbol}")

    def _canonical(self, coin):
        """Return the exchange's exact coin spelling for a position/order row."""
        return self._coins.get(str(coin or "").upper(), coin)

    def _asset(self, coin):
        return self.info.coin_to_asset[coin]

    def _sz_decimals(self, coin):
        return int(self.info.asset_to_sz_decimals[self._asset(coin)])

    def _round_size(self, coin, amount):
        decimals = self._sz_decimals(coin)
        return float(f"{float(amount):.{decimals}f}")

    def _round_price(self, coin, price):
        # Perp prices keep at most 5 significant figures and 6 - szDecimals
        # decimal places (same rule the SDK uses for slippage prices).
        decimals = max(0, 6 - self._sz_decimals(coin))
        return round(float(f"{float(price):.5g}"), decimals)

    @staticmethod
    def _dex(coin):
        return coin.split(":")[0] if ":" in coin else ""

    @_serialized
    def _mark_price(self, coin):
        mids = self.info.all_mids(self._dex(coin)) or {}
        value = mids.get(coin)
        if value is None:
            raise ValueError(f"No Hyperliquid price for {coin}")
        return float(value)

    def validate_symbol_map(self):
        """Fail at startup when a mapping points at an unknown Hyperliquid coin."""
        errors = []
        for name, target in self.symbol_map.entries().items():
            try:
                self._coin(target or name)
            except ValueError as exc:
                errors.append(f"{name} -> {target}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))

    # -- account reads -----------------------------------------------------
    @_serialized
    def _positions_for_dex(self, dex):
        state = self._user_state(dex)
        rows = []
        for item in state.get("assetPositions") or []:
            position = item.get("position") or item
            size = _float(position.get("szi"))
            if not size:
                continue
            entry = _float(position.get("entryPx"))
            notional = abs(_float(position.get("positionValue"))) or abs(size * entry)
            leverage = position.get("leverage") or {}
            leverage = _float(leverage.get("value")) if isinstance(leverage, dict) else _float(leverage)
            rows.append({
                "symbol": self._canonical(position.get("coin")),
                "contracts": abs(size),
                "contractSize": 1,
                "entryPrice": entry,
                "markPrice": notional / abs(size) if size else 0.0,
                "unrealizedPnl": _float(position.get("unrealizedPnl")),
                "side": "long" if size > 0 else "short",
                "notional": notional,
                "leverage": leverage or 1,
                "info": position,
            })
        return rows

    def _fetch_positions(self):
        rows = self._positions_for_dex("")
        for dex in self.dexes[1:]:
            try:
                rows.extend(self._positions_for_dex(dex))
            except Exception:
                log.warning("Failed to read Hyperliquid positions for dex %s", dex, exc_info=True)
        return rows

    async def fetch_positions(self):
        return await asyncio.to_thread(self._fetch_positions)

    def _fetch_position_detail(self, symbol):
        coin = self._coin(symbol)
        for row in self._positions_for_dex(self._dex(coin)):
            if row["symbol"] == coin:
                return row
        return None

    async def fetch_position_detail(self, symbol):
        return await asyncio.to_thread(self._fetch_position_detail, symbol)

    async def position_size(self, symbol):
        detail = await self.fetch_position_detail(symbol)
        if not detail:
            return 0.0
        size = float(detail["contracts"])
        return -size if detail["side"] == "short" else size

    async def entry_quote(self, symbol, side):
        return await asyncio.to_thread(self._mark_price, self._coin(symbol))

    @_serialized
    def _fetch_open_orders(self):
        rows = []
        for dex in self.dexes:
            orders = self._cache_get(self._orders_cache, dex)
            if orders is None:
                try:
                    # frontendOpenOrders carries isTrigger/triggerPx, which the plain
                    # openOrders feed omits; protection cleanup needs them.
                    orders = self._sdk_call(
                        self.info.frontend_open_orders, self.address, dex) or []
                except ClientError as exc:
                    if self._is_transient(exc):
                        log.warning(
                            "Hyperliquid open orders throttled for dex %s", dex or "default")
                        continue
                    try:
                        orders = self._sdk_call(
                            self.info.open_orders, self.address, dex) or []
                    except Exception:
                        log.warning("Failed to read Hyperliquid open orders for dex %s",
                                    dex or "default", exc_info=True)
                        continue
                except Exception:
                    log.warning("Failed to read Hyperliquid open orders for dex %s",
                                dex or "default", exc_info=True)
                    continue
                self._cache_put(self._orders_cache, dex, orders)
            for order in orders:
                rows.append({
                    "id": str(order.get("oid")),
                    "symbol": self._canonical(order.get("coin")),
                    "side": "buy" if str(order.get("side")).upper() == "B" else "sell",
                    "amount": _float(order.get("sz")),
                    "price": _float(order.get("limitPx")),
                    "type": "trigger" if (order.get("isTrigger") or order.get("triggerPx")) else "limit",
                    "reduceOnly": bool(order.get("reduceOnly")),
                    "triggerPrice": _float(order.get("triggerPx")),
                    "info": order,
                })
        return rows

    async def fetch_open_orders(self):
        return await asyncio.to_thread(self._fetch_open_orders)

    @_serialized
    def _equity(self):
        # The default dex is authoritative; HIP-3 dexes keep separate margin,
        # so sum them for a whole-account view.
        state = self._user_state("")
        total = _float((state.get("marginSummary") or {}).get("accountValue"))
        for dex in self.dexes[1:]:
            try:
                state = self._user_state(dex)
            except Exception:
                log.warning("Failed to read Hyperliquid account value for dex %s", dex, exc_info=True)
                continue
            total += _float((state.get("marginSummary") or {}).get("accountValue"))
        if self.unified:
            # In a unified account, perp margin is drawn from spot USDC; the
            # perp accountValue only covers the held portion, so add the free
            # (unheld) spot balance to get the usable account value.
            try:
                spot = self._sdk_call(self.info.spot_user_state, self.address)
            except Exception:
                log.warning("Failed to read Hyperliquid spot balance", exc_info=True)
                spot = {}
            for balance in spot.get("balances") or []:
                if str(balance.get("coin")).upper() == "USDC":
                    free = _float(balance.get("total")) - _float(balance.get("hold"))
                    total += max(0.0, free)
        return total

    async def equity(self):
        return await asyncio.to_thread(self._equity)

    # -- performance / ledger reads ---------------------------------------
    @staticmethod
    def _to_ms(value):
        """Normalize a seconds-or-milliseconds timestamp to milliseconds."""
        number = float(value)
        return int(number * 1000) if abs(number) < 1e11 else int(number)

    def _window(self, since, until, lookback_days):
        """Resolve the requested range to (start_ms, end_ms, backfill)."""
        now_ms = int(time.time() * 1000)
        end_ms = self._to_ms(until) if until is not None else now_ms
        backfill = since is None
        if backfill:
            days = float(lookback_days or 0) or 30.0
            start_ms = end_ms - int(days * 86400 * 1000)
        else:
            start_ms = self._to_ms(since)
        if start_ms >= end_ms:
            start_ms = end_ms - 86400 * 1000
        return start_ms, end_ms, backfill

    @staticmethod
    def _windows(start_ms, end_ms, backfill):
        """Split a backfill into day-sized chunks; incremental keeps one window."""
        if not backfill:
            return [(start_ms, end_ms)]
        day = 86400 * 1000
        windows = []
        cursor = start_ms
        while cursor < end_ms:
            following = min(cursor + day, end_ms)
            windows.append((cursor, following))
            cursor = following
        return windows or [(start_ms, end_ms)]

    @staticmethod
    def _raw_key(row):
        tid = row.get("tid")
        if tid not in (None, ""):
            return f"tid:{tid}"
        delta = row.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        if "closedPnl" in row or row.get("oid") is not None:
            return "time:{0}:coin:{1}:oid:{2}".format(
                row.get("time"), row.get("coin"), row.get("oid"))
        # Funding/ledger hashes are frequently all zeros, so the hash alone
        # cannot identify a row: include the time and the delta payload too.
        coin = row.get("coin") or delta.get("coin")
        kind = delta.get("type") or row.get("type")
        amount = delta.get("usdc")
        if amount is None:
            amount = delta.get("ustd")
        if amount is None:
            amount = delta.get("amount")
        return "hash:{0}:type:{1}:time:{2}:coin:{3}:amount:{4}".format(
            row.get("hash"), kind, row.get("time"), coin, amount)

    @_serialized
    def _paged(self, fetch, start_ms, end_ms, page_size=2000, min_window_ms=60000):
        """Fetch a window, bisecting it when the venue's page cap is reached."""
        if end_ms <= start_ms:
            return []
        rows = list(fetch(start_ms, end_ms) or [])
        if len(rows) >= page_size and (end_ms - start_ms) > min_window_ms:
            middle = (start_ms + end_ms) // 2
            rows = (
                self._paged(fetch, start_ms, middle, page_size, min_window_ms)
                + self._paged(fetch, middle, end_ms, page_size, min_window_ms)
            )
        deduped = {}
        for row in rows:
            deduped.setdefault(self._raw_key(row), row)
        return sorted(deduped.values(), key=lambda item: _float(item.get("time")))

    def _fills_fetch(self, start_ms, end_ms):
        return self.info.user_fills_by_time(
            self.address, start_ms, end_ms, aggregate_by_time=False)

    def _funding_fetch(self, start_ms, end_ms):
        return self.info.user_funding_history(self.address, start_ms, end_ms)

    def _ledger_fetch(self, start_ms, end_ms):
        return self.info.user_non_funding_ledger_updates(self.address, start_ms, end_ms)

    @staticmethod
    def _fill_row(raw):
        tid = raw.get("tid")
        if tid is not None:
            row_id = f"hl:fill:{tid}"
        else:
            row_id = "hl:fill:{0}:{1}:{2}:{3}".format(
                raw.get("hash"), raw.get("oid"), raw.get("time"), raw.get("coin"))
        oid = raw.get("oid")
        return {
            "id": row_id,
            "symbol": raw.get("coin"),
            "side": "buy" if str(raw.get("side")).upper() == "B" else "sell",
            "amount": _float(raw.get("sz")),
            "price": _float(raw.get("px")),
            "realizedPnl": _float(raw.get("closedPnl")),
            "fee": _float(raw.get("fee")),
            "funding": 0.0,
            "order_id": str(oid) if oid is not None else None,
            "source": "hl_fill",
            "timestamp": _float(raw.get("time")) / 1000.0,
        }

    @staticmethod
    def _funding_row(raw):
        delta = raw.get("delta") or {}
        coin = raw.get("coin") or delta.get("coin")
        amount = _float(delta.get("usdc"))
        # ``hash`` is often all zeros; always append time/coin/amount so each
        # payment stays distinct (and survives _paged dedupe).
        identifier = "{0}:{1}:{2}:{3}".format(
            raw.get("hash"), coin, raw.get("time"), amount)
        return {
            "id": f"hl:funding:{identifier}",
            "symbol": coin,
            "side": "",
            "amount": 0.0,
            "price": 0.0,
            "realizedPnl": 0.0,
            "fee": 0.0,
            "funding": amount,
            "order_id": None,
            "source": "hl_funding",
            "timestamp": _float(raw.get("time")) / 1000.0,
        }

    @staticmethod
    def _ledger_type(raw):
        delta = raw.get("delta") or {}
        return str(delta.get("type") or raw.get("type") or "")

    @staticmethod
    def _cash_flow_row(raw):
        delta = raw.get("delta") or {}
        amount = delta.get("usdc")
        if amount is None:
            amount = delta.get("ustd")
        if amount is None:
            amount = delta.get("amount")
        amount = _float(amount)
        # Ledger rows carry ``type`` inside ``delta``, not at the top level.
        kind = str(delta.get("type") or raw.get("type") or "")
        if kind.lower() == "withdraw":
            amount = -abs(amount)
        # Zero hashes are common; time/type/amount keep distinct entries apart.
        identifier = "{0}:{1}:{2}:{3}".format(
            raw.get("hash"), kind, raw.get("time"), amount)
        return {
            "id": f"hl:ledger:{identifier}",
            "kind": kind,
            "amount": amount,
            "timestamp": _float(raw.get("time")) / 1000.0,
        }

    @staticmethod
    def _dedupe_sorted(rows):
        deduped = {}
        for row in rows:
            deduped.setdefault(row["id"], row)
        return sorted(deduped.values(), key=lambda item: item["timestamp"])

    async def fetch_performance_rows(self, since=None, until=None, lookback_days=30,
                                     page_size=None):
        """Return fills and funding as console trade rows (timestamps in seconds)."""
        start_ms, end_ms, backfill = self._window(since, until, lookback_days)
        page_size = int(page_size or self.performance_page_size)
        fills, funding = [], []
        for win_start, win_end in self._windows(start_ms, end_ms, backfill):
            raw_fills = await asyncio.to_thread(
                self._paged, self._fills_fetch, win_start, win_end, page_size)
            raw_funding = await asyncio.to_thread(
                self._paged, self._funding_fetch, win_start, win_end, page_size)
            # Spot fills have no closedPnl; only perp closes/entries carry it.
            fills.extend(self._fill_row(item) for item in raw_fills
                         if "closedPnl" in item)
            funding.extend(self._funding_row(item) for item in raw_funding)
            if backfill:
                await asyncio.sleep(0.25)
        return self._dedupe_sorted(fills + funding)

    async def fetch_realized_pnl(self, since=None, until=None, lookback_days=30,
                                 page_size=None):
        """Compatibility alias: the Console only knows ``fetch_realized_pnl``."""
        return await self.fetch_performance_rows(
            since=since, until=until, lookback_days=lookback_days, page_size=page_size)

    async def fetch_cash_flows(self, since=None, until=None, lookback_days=30,
                               page_size=None, types=None):
        """Return deposit/withdraw ledger rows (timestamps in seconds)."""
        allowed = {str(item).lower() for item in (types or self.cash_flow_types)}
        start_ms, end_ms, backfill = self._window(since, until, lookback_days)
        page_size = int(page_size or self.performance_page_size)
        rows = []
        for win_start, win_end in self._windows(start_ms, end_ms, backfill):
            raw_rows = await asyncio.to_thread(
                self._paged, self._ledger_fetch, win_start, win_end, page_size)
            rows.extend(self._cash_flow_row(item) for item in raw_rows
                        if self._ledger_type(item).lower() in allowed)
            if backfill:
                await asyncio.sleep(0.25)
        return self._dedupe_sorted(rows)

    @_serialized
    def _fetch_order(self, order_id, symbol=None):
        raw = self.info.query_order_by_oid(self.address, int(order_id))
        if not isinstance(raw, dict):
            raise RuntimeError(f"unexpected Hyperliquid order status response: {raw!r}")
        query_status = str(raw.get("status") or "").lower()
        if query_status == "unknownoid":
            # The venue genuinely no longer knows this order id.
            return None
        if query_status != "order":
            # A throttled or malformed reply must not look like a vanished
            # order: the caller would forget a still-resting mirror and, in
            # the worst case, book a phantom fill against the live position.
            raise RuntimeError(f"Hyperliquid order status query failed: {raw!r}")
        payload = raw.get("order") or {}
        order = payload.get("order") or {}
        status = str(payload.get("status") or order.get("status") or "").lower()
        # HL reports ``sz`` as the unfilled remainder and ``origSz`` as placed.
        remaining = max(0.0, _float(order.get("sz")))
        original = _float(order.get("origSz")) or remaining
        filled = max(0.0, original - remaining)
        if status == "filled":
            filled = filled or original
            remaining = 0.0
            normalized = "closed"
        elif status in ("canceled", "cancelled", "expired", "rejected", "margincanceled"):
            normalized = "canceled"
        else:
            normalized = "open"
        return {
            "status": normalized,
            "filled": filled,
            "remaining": remaining,
            "average": _float(order.get("avgPx")) or None,
            "price": _float(order.get("limitPx")) or None,
        }

    async def fetch_order(self, order_id, symbol=None):
        return await asyncio.to_thread(self._fetch_order, order_id, symbol)

    # -- orders ------------------------------------------------------------
    @_serialized
    def _ensure_leverage(self, coin, leverage):
        maximum = self._max_leverage.get(coin.upper()) or 0
        effective = min(int(leverage), maximum) if maximum else int(leverage)
        if effective <= 0 or self._leverage_set.get(coin) == effective:
            return
        try:
            self.client.update_leverage(effective, coin, is_cross=True)
            self._leverage_set[coin] = effective
        except Exception:
            log.warning("Failed to set Hyperliquid leverage %sx for %s", effective, coin, exc_info=True)

    @staticmethod
    def _parse_order(result):
        if not isinstance(result, dict):
            raise RuntimeError(f"unexpected Hyperliquid order response: {result!r}")
        if result.get("status") == "err":
            message = str(result.get("response") or result)
            if _small_order(message):
                raise OrderQuantityTooSmall(message)
            raise RuntimeError(message)
        response = result.get("response") or {}
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        if not statuses:
            message = str(response or result)
            if _small_order(message):
                raise OrderQuantityTooSmall(message)
            raise RuntimeError(f"Hyperliquid order rejected: {message}")
        status = statuses[0]
        if "error" in status:
            message = str(status["error"])
            if _small_order(message):
                raise OrderQuantityTooSmall(message)
            raise RuntimeError(message)
        if "filled" in status:
            filled = status["filled"] or {}
            return {"oid": filled.get("oid"), "filled": _float(filled.get("totalSz")),
                    "avgPx": _float(filled.get("avgPx")), "resting": False}
        if "resting" in status:
            return {"oid": (status["resting"] or {}).get("oid"),
                    "filled": 0.0, "avgPx": 0.0, "resting": True}
        return {"oid": None, "filled": 0.0, "avgPx": 0.0, "resting": False}

    @_serialized
    def _create_order(self, symbol, side, amount, order_type, price, reduce_only, post_only=False):
        coin = self._coin(symbol)
        size = self._round_size(coin, amount)
        if size <= 0:
            raise OrderQuantityTooSmall(f"quantity {amount:g} rounds to zero")
        if order_type == "limit" and (price is None or float(price) <= 0):
            raise ValueError("limit price must be positive")
        if post_only and order_type != "limit":
            raise ValueError("post_only requires a limit order")
        is_buy = side == "buy"
        leverage = self.config.get("leverage")
        if leverage and not reduce_only:
            self._ensure_leverage(coin, int(leverage))
        if order_type == "limit":
            limit_px = self._round_price(coin, float(price))
            result = self.client.order(
                coin, is_buy, size, limit_px, {"limit": {"tif": "Alo" if post_only else "Gtc"}},
                reduce_only=reduce_only,
            )
        else:
            mid = self._mark_price(coin)
            limit_px = mid * (1 + self.slippage) if is_buy else mid * (1 - self.slippage)
            limit_px = self._round_price(coin, limit_px)
            result = self.client.order(
                coin, is_buy, size, limit_px, {"limit": {"tif": "Ioc"}}, reduce_only=reduce_only,
            )
        status = self._parse_order(result)
        self._invalidate_account_cache()
        filled = status["filled"] if not status["resting"] else size
        return Order(
            str(status["oid"] or ""), symbol, side, filled or size,
            status["avgPx"] or (float(price) if price else limit_px),
            "open" if status["resting"] else "closed", reduce_only,
        )

    async def create_order(self, symbol, side, amount, order_type="market", price=None,
                           reduce_only=False, post_only=False):
        return await asyncio.to_thread(
            self._create_order, symbol, side, amount, order_type, price, reduce_only, post_only,
        )

    @staticmethod
    def _check_cancel(result):
        """Raise ``OrderGone`` after a cancel the venue reports as already gone.

        Callers need to distinguish a real cancellation from an order that had
        already filled (or was already cancelled): the fill still has to be
        booked.  Previously this silently returned for "already gone", which
        made the recovery paths in ``Engine._cancel_pending`` and
        ``OrderMirror._cancel`` unreachable with the live adapter.
        """
        if not isinstance(result, dict):
            return
        if result.get("status") == "err":
            message = str(result.get("response") or result)
            if _order_gone(message):
                raise OrderGone(message)
            raise RuntimeError(message)
        response = result.get("response") or {}
        data = response.get("data") if isinstance(response, dict) else None
        statuses = data.get("statuses") if isinstance(data, dict) else None
        errors = []
        for item in statuses or []:
            if isinstance(item, dict):
                if item.get("error"):
                    errors.append(str(item["error"]))
            elif isinstance(item, str) and item.lower() not in (
                    "success", "waitingforfill"):
                errors.append(item)
        if not errors:
            return
        message = "; ".join(errors)
        if all(_order_gone(item) for item in errors):
            raise OrderGone(message)
        raise RuntimeError(message)

    @_serialized
    def _cancel_order(self, order_id, symbol=None):
        coin = self._coin(symbol) if symbol else None
        if not coin:
            for order in self._fetch_open_orders():
                if order["id"] == str(order_id):
                    coin = order["symbol"]
                    break
        if not coin:
            raise ValueError(f"cannot resolve Hyperliquid coin for order {order_id}")
        self._check_cancel(self.client.cancel(coin, int(order_id)))
        self._invalidate_account_cache()

    async def cancel_order(self, order_id, symbol=None):
        await asyncio.to_thread(self._cancel_order, order_id, symbol)

    @_serialized
    def _set_protection(self, symbol, side, amount, trigger_price):
        coin = self._coin(symbol)
        trigger = self._round_price(coin, trigger_price)
        # Use a limit beyond the trigger so the market stop still fills.
        limit_px = trigger * (1 - self.slippage) if side == "sell" else trigger * (1 + self.slippage)
        result = self.client.order(
            coin, side == "buy", self._round_size(coin, amount), self._round_price(coin, limit_px),
            {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True,
        )
        self._invalidate_account_cache()
        return str(self._parse_order(result)["oid"] or "")

    async def set_protection(self, symbol, side, amount, trigger_price):
        return await asyncio.to_thread(self._set_protection, symbol, side, amount, trigger_price)

    async def cancel_protection(self, order_id, symbol):
        await self.cancel_order(order_id, symbol)

    @_serialized
    def _cancel_protection_orders(self, symbol, keep_id=None):
        coin = self._coin(symbol)
        for order in self._fetch_open_orders():
            if order["symbol"] != coin:
                continue
            if keep_id and order["id"] == str(keep_id):
                continue
            if order["type"] == "trigger" and order["reduceOnly"]:
                try:
                    self._check_cancel(self.client.cancel(coin, int(order["id"])))
                except Exception as exc:
                    if not _order_gone(exc):
                        log.warning("Failed to cancel protection order %s", order["id"], exc_info=True)
        self._invalidate_account_cache()

    async def cancel_protection_orders(self, symbol, keep_id=None):
        await asyncio.to_thread(self._cancel_protection_orders, symbol, keep_id)

    @_serialized
    def _cancel_all_orders(self):
        canceled = 0
        for order in self._fetch_open_orders():
            try:
                self._check_cancel(self.client.cancel(order["symbol"], int(order["id"])))
                canceled += 1
            except Exception as exc:
                if not _order_gone(exc):
                    log.warning("Failed to cancel Hyperliquid order %s", order["id"], exc_info=True)
        self._invalidate_account_cache()
        return canceled

    async def cancel_all_orders(self):
        return await asyncio.to_thread(self._cancel_all_orders)

    def _has_open_positions(self):
        return any(position["contracts"] > 0 for position in self._fetch_positions())

    async def has_open_positions(self):
        return await asyncio.to_thread(self._has_open_positions)

    def _close_all_positions(self, attempts):
        closed = 0
        last_error = None
        for attempt in range(attempts):
            try:
                self._cancel_all_orders()
            except Exception:
                log.warning("Failed to cancel orders before flattening", exc_info=True)
            positions = self._fetch_positions()
            if not positions:
                return closed
            for position in positions:
                coin = position["symbol"]
                size = float(position["contracts"])
                side = "sell" if position["side"] == "long" else "buy"
                try:
                    self._create_order(coin, side, size, "market", None, True)
                    closed += 1
                except Exception as exc:
                    last_error = exc
                    log.warning("Failed to close %s (%g)", coin, size, exc_info=True)
            if attempt < attempts - 1:
                time.sleep(1)
        remaining = self._fetch_positions()
        if remaining:
            raise RuntimeError(
                "failed to flatten position(s): "
                + ", ".join(str(position["symbol"]) for position in remaining)
            ) from last_error
        return closed

    async def close_all_positions(self, attempts=3):
        return await asyncio.to_thread(self._close_all_positions, attempts)
